"""交易登记：文本解析、预览、确认入账。"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.portfolio_service import normalize_symbol, recompute_positions
from app.storage.models import (
    LedgerEntry,
    PendingAction,
    PendingStatus,
    Portfolio,
    TradeDirection,
)

ZERO = Decimal("0")

_DIR_PATTERNS: list[tuple[TradeDirection, re.Pattern[str]]] = [
    (TradeDirection.buy, re.compile(r"买入|购入|已?买|buy", re.IGNORECASE)),
    (TradeDirection.sell, re.compile(r"卖出|已?卖|sell", re.IGNORECASE)),
    (TradeDirection.deposit, re.compile(r"入金|转入|存入|deposit")),
    (TradeDirection.withdraw, re.compile(r"出金|转出|取出|withdraw")),
    (TradeDirection.dividend, re.compile(r"分红|派息|股息|dividend")),
]
_SYMBOL_RE = re.compile(r"(\d{6})(?:\.(SH|SZ|BJ))?", re.IGNORECASE)
_SUFFIXED_SYM_RE = re.compile(r"\d{6}\.(?:SH|SZ|BJ)", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_FEE_RE = re.compile(r"费用\s*(\d+(?:\.\d+)?)")
_DATE_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2})")


@dataclass
class TradePreview:
    direction: TradeDirection
    symbol: str | None
    quantity: Decimal | None
    price: Decimal | None
    amount: Decimal | None
    fee: Decimal
    trade_date: date
    cash_delta: Decimal


def parse_trade_text(text: str) -> TradePreview | str:
    """解析交易描述；返回预览对象或错误说明。"""
    direction = None
    for d, pat in _DIR_PATTERNS:
        if pat.search(text):
            direction = d
            break
    if direction is None:
        return "未识别方向：请包含 买入 / 卖出 / 入金 / 出金 / 分红"
    today = date.today()
    dm = _DATE_RE.search(text)
    trade_date = today
    if dm:
        try:
            parts = [int(x) for x in dm.group(1).split("-")]
            trade_date = (
                date(parts[0], parts[1], parts[2])
                if len(parts) == 3
                else date(today.year, parts[0], parts[1])
            )
        except ValueError:
            return f"日期无效：{dm.group(1)}"

    fee = ZERO
    fm = _FEE_RE.search(text)
    if fm:
        fee = Decimal(fm.group(1))

    if direction in (TradeDirection.deposit, TradeDirection.withdraw, TradeDirection.dividend):
        # 带后缀代码一律剔除；无后缀 6 位数字（如金额 100000）不能误删
        had_suffixed = bool(_SUFFIXED_SYM_RE.search(text))
        cleaned = _SUFFIXED_SYM_RE.sub("", text)
        cleaned = _DATE_RE.sub("", cleaned)
        cleaned = _FEE_RE.sub("", cleaned)
        nums = [Decimal(m) for m in _NUM_RE.findall(cleaned)]
        if direction == TradeDirection.dividend and not had_suffixed and len(nums) >= 2:
            nums = nums[1:]  # 首个数字视为裸代码，第二个才是金额
        amount = nums[0] if nums else None
        if amount is None or amount <= 0:
            return "请提供金额，例如：入金 100000"
        sign = Decimal("-1") if direction == TradeDirection.withdraw else Decimal("1")
        return TradePreview(
            direction=direction, symbol=None, quantity=None, price=None,
            amount=amount, fee=fee, trade_date=trade_date, cash_delta=sign * amount,
        )

    sm = _SYMBOL_RE.search(text)
    if sm is None:
        return "未识别股票代码（6位数字，可带 .SH/.SZ）"
    symbol = normalize_symbol(sm.group(0))
    cleaned = _SYMBOL_RE.sub("", text)
    cleaned = _DATE_RE.sub("", cleaned)
    cleaned = _FEE_RE.sub("", cleaned)
    nums = [Decimal(m) for m in _NUM_RE.findall(cleaned)]
    quantity = nums[0] if len(nums) >= 1 else None
    price = nums[1] if len(nums) >= 2 else None
    if quantity is None or quantity <= 0 or price is None or price <= 0:
        return "请提供数量和成交价，例如：买入 600036.SH 100 32.50 费用5"
    gross = quantity * price
    cash_delta = -(gross + fee) if direction == TradeDirection.buy else (gross - fee)
    return TradePreview(
        direction=direction, symbol=symbol, quantity=quantity, price=price,
        amount=None, fee=fee, trade_date=trade_date, cash_delta=cash_delta,
    )


async def confirm_trade(
    session: AsyncSession,
    pending: PendingAction,
    portfolio: Portfolio,
) -> LedgerEntry:
    """事务内：锁组合行 → 写分录 → 重算持仓 → 标记确认。"""
    await session.execute(
        select(Portfolio.id).where(Portfolio.id == portfolio.id).with_for_update()
    )
    p = pending.payload
    direction = TradeDirection(p["direction"])
    entry = LedgerEntry(
        portfolio_id=portfolio.id,
        trade_date=date.fromisoformat(p["trade_date"]),
        direction=direction,
        symbol=p.get("symbol"),
        quantity=Decimal(p["quantity"]) if p.get("quantity") is not None else None,
        price=Decimal(p["price"]) if p.get("price") is not None else None,
        gross_amount=Decimal(p["gross_amount"]) if p.get("gross_amount") is not None else None,
        fee=Decimal(p.get("fee", "0")),
        cash_delta=Decimal(p["cash_delta"]),
        currency="CNY",
        source="manual",
        idempotency_key=f"pa:{pending.token}",
        note=p.get("note"),
    )
    session.add(entry)
    await session.flush()
    await recompute_positions(session, portfolio.id)
    pending.status = PendingStatus.confirmed
    pending.confirmed_at = datetime.now(timezone.utc)
    await session.flush()
    return entry
