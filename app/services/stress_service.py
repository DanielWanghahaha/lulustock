"""压力测试：确定性情景重估（β=1 假设）。"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ledger import EntryRow, compute_cash
from app.services.quotes_service import latest_closes
from app.storage.models import LedgerEntry, PolicyVersion, Position, Portfolio


@dataclass
class StressResult:
    drop_pct: float
    shocked_nav: Decimal
    loss: Decimal
    loss_pct: float
    cash_weight_after: float
    breach: bool


async def _tolerance(session: AsyncSession, user_id: int) -> float:
    row = (
        await session.execute(
            select(PolicyVersion)
            .where(PolicyVersion.user_id == user_id, PolicyVersion.status.in_(["active", "proposed"]))
            .order_by(PolicyVersion.status.desc(), PolicyVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return float((row.payload or {}).get("max_drawdown_tolerance", 0.20)) if row else 0.20


async def run_stress(
    session: AsyncSession, portfolio: Portfolio, user_id: int, drops: list[float] | None = None
) -> tuple[dict, list[StressResult], list[str]]:
    positions = (
        (await session.execute(select(Position).where(Position.portfolio_id == portfolio.id)))
        .scalars()
        .all()
    )
    closes = await latest_closes(session, [p.symbol for p in positions])
    entries = (
        (await session.execute(select(LedgerEntry).where(LedgerEntry.portfolio_id == portfolio.id)))
        .scalars()
        .all()
    )
    cash = compute_cash(
        [EntryRow(e.trade_date, e.direction, e.symbol, e.quantity, e.price, e.fee, e.cash_delta) for e in entries]
    )
    missing = [p.symbol for p in positions if p.symbol not in closes]
    market_value = sum((p.quantity * closes[p.symbol][1] for p in positions if p.symbol in closes), Decimal("0"))
    nav = cash + market_value

    tolerance = await _tolerance(session, user_id)
    drops = drops or [0.10, 0.20, 0.30]
    results: list[StressResult] = []
    for drop in drops:
        shocked_mv = sum(
            (p.quantity * closes[p.symbol][1] * Decimal(str(1 - drop)) for p in positions if p.symbol in closes),
            Decimal("0"),
        )
        shocked_nav = cash + shocked_mv
        loss = nav - shocked_nav
        loss_pct = float(loss / nav * 100) if nav > 0 else 0.0
        cash_weight = float(cash / shocked_nav * 100) if shocked_nav > 0 else 0.0
        results.append(
            StressResult(
                drop_pct=drop * 100,
                shocked_nav=shocked_nav,
                loss=loss,
                loss_pct=loss_pct,
                cash_weight_after=cash_weight,
                breach=loss_pct >= tolerance * 100,
            )
        )
    base = {
        "cash": cash,
        "market_value": market_value,
        "nav": nav,
        "missing": missing,
        "tolerance_pct": tolerance * 100,
        "positions": len(positions),
    }
    return base, results, missing
