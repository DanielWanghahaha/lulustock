"""持仓：CSV 导入校验、确认入账、组合快照。"""
import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ledger import EntryRow, aggregate_positions, compute_cash
from app.storage.models import (
    ImportBatch,
    ImportItem,
    LedgerEntry,
    PendingAction,
    PendingStatus,
    Position,
    Portfolio,
    TradeDirection,
)

MAX_IMPORT_ROWS = 100


def normalize_symbol(raw: str) -> str | None:
    """标准化 A 股代码：600036.SH / 000001.SZ / 830799.BJ。"""
    s = raw.strip().upper()
    code, _, suffix = s.partition(".")
    if not (code.isdigit() and len(code) == 6):
        return None
    if not suffix:
        if code.startswith(("6", "9")):
            suffix = "SH"
        elif code.startswith(("0", "2", "3")):
            suffix = "SZ"
        elif code.startswith(("4", "8")):
            suffix = "BJ"
        else:
            return None
    if suffix not in ("SH", "SZ", "BJ"):
        return None
    return f"{code}.{suffix}"


@dataclass
class ImportRow:
    row_no: int
    symbol: str
    quantity: Decimal
    cost_per_share: Decimal
    fee: Decimal
    currency: str
    as_of: date


def parse_import_csv(content: str) -> tuple[list[ImportRow], list[str]]:
    """解析并校验 CSV；返回 (有效行, 错误列表)。"""
    errors: list[str] = []
    rows: list[ImportRow] = []
    reader = csv.DictReader(io.StringIO(content))
    required = {"symbol", "quantity", "cost_per_share", "currency", "as_of"}
    if reader.fieldnames is None:
        return [], ["CSV 为空或无表头"]
    header = {h.strip().lower() for h in reader.fieldnames}
    missing = required - header
    if missing:
        return [], [f"缺少列：{', '.join(sorted(missing))}"]
    seen_symbols: set[str] = set()
    for i, raw in enumerate(reader, start=2):
        try:
            symbol = normalize_symbol(raw.get("symbol") or "")
            if symbol is None:
                errors.append(f"第{i}行：股票代码无效（{raw.get('symbol')}）")
                continue
            if symbol in seen_symbols:
                errors.append(f"第{i}行：{symbol} 重复出现")
                continue
            seen_symbols.add(symbol)
            quantity = Decimal((raw.get("quantity") or "").strip())
            cost = Decimal((raw.get("cost_per_share") or "").strip())
            fee = Decimal((raw.get("fee") or "0").strip() or "0")
            if quantity <= 0:
                errors.append(f"第{i}行：数量必须大于 0")
                continue
            if cost <= 0:
                errors.append(f"第{i}行：成本价必须大于 0")
                continue
            currency = (raw.get("currency") or "").strip().upper()
            if currency != "CNY":
                errors.append(f"第{i}行：币种仅支持 CNY（得到 {currency or '空'}）")
                continue
            as_of = date.fromisoformat((raw.get("as_of") or "").strip())
            rows.append(ImportRow(i, symbol, quantity, cost, fee, currency, as_of))
        except (InvalidOperation, ValueError) as exc:
            errors.append(f"第{i}行：数值/日期格式错误（{exc}）")
        if len(rows) + len(errors) > MAX_IMPORT_ROWS:
            errors.append(f"超过单次导入上限 {MAX_IMPORT_ROWS} 行")
            break
    return rows, errors


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def confirm_import(
    session: AsyncSession,
    pending: PendingAction,
    portfolio: Portfolio,
    rows: list[ImportRow],
    file_hash: str,
) -> int:
    """事务内：锁定组合行 → 写批次/明细/seed 分录 → 重算持仓。返回写入分录数。"""
    await session.execute(
        select(Portfolio.id).where(Portfolio.id == portfolio.id).with_for_update()
    )
    batch = ImportBatch(
        user_id=pending.user_id,
        portfolio_id=portfolio.id,
        file_hash=file_hash,
        status="confirmed",
        row_count=len(rows),
        confirmed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(batch)
    await session.flush()
    n = 0
    for r in rows:
        session.add(
            ImportItem(
                batch_id=batch.id,
                row_no=r.row_no,
                symbol=r.symbol,
                quantity=r.quantity,
                cost_per_share=r.cost_per_share,
                fee=r.fee,
                currency=r.currency,
                as_of=r.as_of,
                status="ok",
            )
        )
        session.add(
            LedgerEntry(
                portfolio_id=portfolio.id,
                trade_date=r.as_of,
                direction=TradeDirection.seed,
                symbol=r.symbol,
                quantity=r.quantity,
                price=r.cost_per_share,
                gross_amount=r.quantity * r.cost_per_share,
                fee=r.fee,
                cash_delta=Decimal("0"),
                currency=r.currency,
                source="csv",
                idempotency_key=f"pa:{pending.token}:{r.row_no}",
                note="CSV 初始建仓",
            )
        )
        n += 1
    await recompute_positions(session, portfolio.id)
    pending.status = PendingStatus.confirmed
    pending.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await session.flush()
    return n


async def recompute_positions(session: AsyncSession, portfolio_id: int) -> None:
    """从总账全量重算持仓投影。"""
    result = await session.execute(
        select(LedgerEntry)
        .where(LedgerEntry.portfolio_id == portfolio_id, LedgerEntry.symbol.is_not(None))
        .order_by(LedgerEntry.trade_date, LedgerEntry.id)
    )
    entries = [
        EntryRow(
            trade_date=e.trade_date,
            direction=e.direction,
            symbol=e.symbol,
            quantity=e.quantity,
            price=e.price,
            fee=e.fee,
            cash_delta=e.cash_delta,
        )
        for e in result.scalars()
    ]
    aggs = aggregate_positions(entries)
    pos_rows = (
        (await session.execute(select(Position).where(Position.portfolio_id == portfolio_id)))
        .scalars()
        .all()
    )
    by_symbol = {p.symbol: p for p in pos_rows}
    seen: set[str] = set()
    for sym, agg in aggs.items():
        seen.add(sym)
        if sym in by_symbol:
            p = by_symbol[sym]
            p.quantity = agg.quantity
            p.avg_cost = agg.avg_cost
            p.total_cost = agg.total_cost
        else:
            session.add(
                Position(
                    portfolio_id=portfolio_id,
                    symbol=sym,
                    quantity=agg.quantity,
                    avg_cost=agg.avg_cost,
                    total_cost=agg.total_cost,
                )
            )
    for sym, p in by_symbol.items():
        if sym not in seen or p.quantity <= 0:
            await session.execute(delete(Position).where(Position.id == p.id))
    await session.flush()


async def portfolio_snapshot(session: AsyncSession, portfolio_id: int) -> tuple[list[Position], Decimal]:
    """读取持仓投影与现金余额。"""
    positions = (
        (
            await session.execute(
                select(Position).where(Position.portfolio_id == portfolio_id).order_by(Position.symbol)
            )
        )
        .scalars()
        .all()
    )
    result = await session.execute(select(LedgerEntry).where(LedgerEntry.portfolio_id == portfolio_id))
    entries = [
        EntryRow(
            trade_date=e.trade_date,
            direction=e.direction,
            symbol=e.symbol,
            quantity=e.quantity,
            price=e.price,
            fee=e.fee,
            cash_delta=e.cash_delta,
        )
        for e in result.scalars()
    ]
    return list(positions), compute_cash(entries)
