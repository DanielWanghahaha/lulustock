"""确定性风险引擎：R01/R04/R05-R08/R10/R11 及数据完整性判断。"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import (
    DailyQuote,
    PolicyVersion,
    PortfolioSnapshot,
    Position,
    WatchlistItem,
)

ZERO = Decimal("0")


@dataclass
class Finding:
    level: str  # info | yellow | red
    code: str
    message: str


async def _risk_limits(session: AsyncSession, user_id: int) -> dict:
    row = (
        await session.execute(
            select(PolicyVersion)
            .where(PolicyVersion.user_id == user_id, PolicyVersion.status.in_(["active", "proposed"]))
            .order_by(PolicyVersion.status.desc(), PolicyVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"max_single_stock_weight": 0.08, "min_liquid_reserve_weight": 0.15}
    return (row.payload or {}).get("risk_limits", {})


async def evaluate_portfolio(
    session: AsyncSession,
    user_id: int,
    portfolio_id: int,
    latest_trading_day: date | None,
) -> tuple[list[Finding], dict]:
    """基于持仓 + 最新收盘评估；返回 (findings, 摘要数据)。"""
    findings: list[Finding] = []
    positions = (
        (await session.execute(select(Position).where(Position.portfolio_id == portfolio_id)))
        .scalars()
        .all()
    )
    closes: dict[str, tuple[date, Decimal]] = {}
    for p in positions:
        row = (
            await session.execute(
                select(DailyQuote)
                .where(DailyQuote.symbol == p.symbol)
                .order_by(DailyQuote.trade_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            closes[p.symbol] = (row.trade_date, row.close)
    # 现金
    from sqlalchemy import select as _s

    from app.domain.ledger import EntryRow, compute_cash
    from app.storage.models import LedgerEntry

    entries = (
        (await session.execute(_s(LedgerEntry).where(LedgerEntry.portfolio_id == portfolio_id)))
        .scalars()
        .all()
    )
    cash = compute_cash(
        [EntryRow(e.trade_date, e.direction, e.symbol, e.quantity, e.price, e.fee, e.cash_delta) for e in entries]
    )

    stale = [p.symbol for p in positions if p.symbol not in closes]
    if stale:
        findings.append(
            Finding(
                "yellow",
                "R10",
                f"以下持仓缺少有效行情，精确市值与仓位不可用：{', '.join(stale)}",
            )
        )

    market_value = sum((p.quantity * closes[p.symbol][1] for p in positions if p.symbol in closes), ZERO)
    nav = cash + market_value
    summary = {
        "cash": cash,
        "market_value": market_value,
        "nav": nav,
        "stale_symbols": stale,
        "weights": {},
    }

    if nav > 0 and not stale:
        limits = await _risk_limits(session, user_id)
        max_single = Decimal(str(limits.get("max_single_stock_weight", 0.08)))
        min_reserve = Decimal(str(limits.get("min_liquid_reserve_weight", 0.15)))
        for p in positions:
            w = (p.quantity * closes[p.symbol][1]) / nav
            summary["weights"][p.symbol] = float(w.quantize(Decimal("0.0001")))
            if w > max_single:
                findings.append(
                    Finding(
                        "yellow",
                        "R01",
                        f"集中度超限：{p.symbol} 仓位 {w * 100:.1f}% > 上限 {max_single * 100:.0f}%（暂停使超限加重的新增建议）",
                    )
                )
        cash_weight = cash / nav
        summary["cash_weight"] = float(cash_weight.quantize(Decimal("0.0001")))
        if cash_weight < min_reserve:
            findings.append(
                Finding(
                    "yellow",
                    "R04",
                    f"流动储备不足：现金 {cash_weight * 100:.1f}% < 下限 {min_reserve * 100:.0f}%（暂停不满足现金约束的新增建议）",
                )
            )
        # 回撤 R05-R08
        snaps = (
            (
                await session.execute(
                    select(PortfolioSnapshot)
                    .where(
                        PortfolioSnapshot.portfolio_id == portfolio_id,
                        PortfolioSnapshot.complete.is_(True),
                    )
                    .order_by(PortfolioSnapshot.trade_date)
                )
            )
            .scalars()
            .all()
        )
        if snaps:
            navs = [s.nav for s in snaps if s.nav > 0]
            if navs:
                peak = max(navs)
                current = navs[-1]
                dd = (peak - current) / peak if peak > 0 else ZERO
                summary["current_drawdown"] = float(dd.quantize(Decimal("0.0001")))
                if dd >= Decimal("0.20"):
                    findings.append(Finding("red", "R08", f"当前回撤 {dd * 100:.1f}% ≥ 20%：紧急复核，不自动清仓"))
                elif dd >= Decimal("0.15"):
                    findings.append(Finding("red", "R07", f"当前回撤 {dd * 100:.1f}% ≥ 15%：暂停新增风险仓位建议"))
                elif dd >= Decimal("0.10"):
                    findings.append(Finding("yellow", "R06", f"当前回撤 {dd * 100:.1f}% ≥ 10%：触发专项诊断"))
                elif dd >= Decimal("0.05"):
                    findings.append(Finding("yellow", "R05", f"当前回撤 {dd * 100:.1f}% ≥ 5%：进入简报"))

    # R11 观察池价格触发
    watch = (
        (await session.execute(select(WatchlistItem).where(WatchlistItem.user_id == user_id)))
        .scalars()
        .all()
    )
    for w in watch:
        c = closes.get(w.symbol)
        if c is None:
            qrow = (
                await session.execute(
                    select(DailyQuote).where(DailyQuote.symbol == w.symbol).order_by(DailyQuote.trade_date.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if qrow is not None:
                c = (qrow.trade_date, qrow.close)
        if c is None:
            continue
        d, close = c
        if w.price_alert_high is not None and close >= w.price_alert_high:
            findings.append(
                Finding("yellow", "R11", f"观察价位触达：{w.symbol} 收盘 {close} ≥ 上限 {w.price_alert_high}（{d}）→ 建议启动估值与基本面复核，不等于买入")
            )
        if w.price_alert_low is not None and close <= w.price_alert_low:
            findings.append(
                Finding("yellow", "R11", f"观察价位触达：{w.symbol} 收盘 {close} ≤ 下限 {w.price_alert_low}（{d}）→ 建议启动估值与基本面复核，不等于买入")
            )
    return findings, summary
