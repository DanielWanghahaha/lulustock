"""行情更新与快照：更新日线、交易日历、组合净值快照。"""
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ledger import EntryRow, compute_cash
from app.providers.quotes import TencentProvider, fetch_closes_with_retry
from app.storage.db import Session
from app.storage.models import (
    DailyQuote,
    LedgerEntry,
    Portfolio,
    PortfolioSnapshot,
    Position,
    TradingCalendar,
    WatchlistItem,
)

log = logging.getLogger(__name__)
PROVIDER = TencentProvider()
HISTORY_DAYS = 60


async def _all_symbols(session: AsyncSession) -> list[str]:
    pos_syms = set(
        (await session.execute(select(Position.symbol).distinct())).scalars().all()
    )
    watch_syms = set(
        (await session.execute(select(WatchlistItem.symbol).distinct())).scalars().all()
    )
    return sorted(pos_syms | watch_syms)


async def update_quotes_for_symbols(session: AsyncSession, symbols: list[str]) -> dict:
    """拉取近 60 天未复权日线并 upsert；返回汇总。"""
    end = date.today()
    begin = end - timedelta(days=HISTORY_DAYS)
    ok, failed, unsupported = 0, 0, 0
    latest_by_symbol: dict[str, tuple[date, Decimal]] = {}
    trading_dates: set[date] = set()
    for symbol in symbols:
        code, _, suffix = symbol.partition(".")
        if suffix not in ("SH", "SZ"):
            unsupported += 1
            continue
        try:
            quotes = await fetch_closes_with_retry(PROVIDER, symbol, begin, end)
        except RuntimeError as exc:
            log.warning("%s", exc)
            failed += 1
            continue
        for q in quotes:
            stmt = mysql_insert(DailyQuote).values(
                symbol=q.symbol,
                trade_date=q.trade_date,
                close=Decimal(str(q.close)),
                currency="CNY",
                source=q.source,
                quality="ok",
                source_url=q.source_url,
                fetched_at=q.fetched_at,
            )
            stmt = stmt.on_duplicate_key_update(
                close=stmt.inserted.close,
                source_url=stmt.inserted.source_url,
                fetched_at=stmt.inserted.fetched_at,
                quality="ok",
            )
            await session.execute(stmt)
            trading_dates.add(q.trade_date)
        if quotes:
            last = max(quotes, key=lambda x: x.trade_date)
            latest_by_symbol[symbol] = (last.trade_date, Decimal(str(last.close)))
            ok += 1
        else:
            failed += 1
    for d in trading_dates:
        cal_stmt = mysql_insert(TradingCalendar).values(trade_date=d, source=PROVIDER.source)
        cal_stmt = cal_stmt.on_duplicate_key_update(source=cal_stmt.inserted.source)
        await session.execute(cal_stmt)
    await session.flush()
    return {
        "ok": ok,
        "failed": failed,
        "unsupported": unsupported,
        "latest": {s: (d.isoformat(), float(v)) for s, (d, v) in latest_by_symbol.items()},
    }


async def update_quotes(session: AsyncSession) -> dict:
    symbols = await _all_symbols(session)
    return await update_quotes_for_symbols(session, symbols)


async def latest_closes(session: AsyncSession, symbols: list[str]) -> dict[str, tuple[date, Decimal]]:
    """每个代码最近一条有效收盘。"""
    result: dict[str, tuple[date, Decimal]] = {}
    for symbol in symbols:
        row = (
            await session.execute(
                select(DailyQuote)
                .where(DailyQuote.symbol == symbol)
                .order_by(DailyQuote.trade_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            result[symbol] = (row.trade_date, row.close)
    return result


async def latest_trading_date(session: AsyncSession) -> date | None:
    row = (
        await session.execute(select(TradingCalendar.trade_date).order_by(TradingCalendar.trade_date.desc()).limit(1))
    ).scalar_one_or_none()
    return row


async def persist_snapshot(session: AsyncSession, portfolio: Portfolio) -> PortfolioSnapshot:
    """计算并保存当日组合快照（市值口径 + 完整性标记）。"""
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
        [
            EntryRow(e.trade_date, e.direction, e.symbol, e.quantity, e.price, e.fee, e.cash_delta)
            for e in entries
        ]
    )
    market_value = Decimal("0")
    complete = True
    for p in positions:
        if p.symbol in closes:
            market_value += p.quantity * closes[p.symbol][1]
        else:
            complete = False
    trade_date = date.today()
    snap = (
        await session.execute(
            select(PortfolioSnapshot).where(
                PortfolioSnapshot.portfolio_id == portfolio.id,
                PortfolioSnapshot.trade_date == trade_date,
            )
        )
    ).scalar_one_or_none()
    if snap is None:
        snap = PortfolioSnapshot(portfolio_id=portfolio.id, trade_date=trade_date)
        session.add(snap)
    snap.cash = cash
    snap.market_value = market_value
    snap.nav = cash + market_value
    snap.complete = complete
    await session.flush()
    return snap
