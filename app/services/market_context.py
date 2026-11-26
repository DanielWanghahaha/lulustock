"""行情统计上下文：为研究提供 52 周高低、涨跌幅、均线等确定性数据。"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.quotes import TencentProvider, fetch_closes_with_retry
from app.services.quotes_service import PROVIDER, HISTORY_DAYS
from app.storage.models import DailyQuote
from sqlalchemy.dialects.mysql import insert as mysql_insert

ZERO = Decimal("0")


async def gather_price_context(session: AsyncSession, symbol: str, days: int = 420) -> dict[str, Any]:
    """拉取约 250 个交易日收盘并计算统计；返回 None 表示无数据。"""
    end = date.today()
    begin = end - timedelta(days=days)
    try:
        quotes = await fetch_closes_with_retry(PROVIDER, symbol, begin, end)
    except RuntimeError:
        quotes = []
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
            fetched_at=stmt.inserted.fetched_at,
        )
        await session.execute(stmt)
    rows = (
        (
            await session.execute(
                select(DailyQuote.trade_date, DailyQuote.close)
                .where(DailyQuote.symbol == symbol)
                .order_by(DailyQuote.trade_date.desc())
                .limit(250)
            )
        )
        .all()
    )
    if not rows:
        return {}
    rows.reverse()  # 升序
    closes = [r[1] for r in rows]
    last_close = closes[-1]
    last_date = rows[-1][0]

    def chg(n: int) -> float | None:
        if len(closes) > n:
            base = closes[-1 - n]
            return float((last_close / base - 1) * 100) if base else None
        return None

    def ma(n: int) -> float | None:
        if len(closes) >= n:
            return float(sum(closes[-n:]) / n)
        return None

    return {
        "symbol": symbol,
        "last_date": last_date.isoformat(),
        "last_close": float(last_close),
        "high_250d": float(max(closes)),
        "low_250d": float(min(closes)),
        "chg_20d_pct": chg(20),
        "chg_60d_pct": chg(60),
        "chg_120d_pct": chg(120),
        "ma20": ma(20),
        "ma60": ma(60),
        "history_points": len(closes),
    }


async def financial_context(symbol: str) -> dict[str, Any] | None:
    """估值快照 + 报告期财务指标（东财公开 F10，未核验）。失败返回 None。"""
    from app.providers.fundamentals import fetch_financial_summary, fetch_valuation_snapshot

    try:
        val = await fetch_valuation_snapshot(symbol)
        periods = await fetch_financial_summary(symbol)
        if not val and not periods:
            return None
        return {"valuation_snapshot": val, "report_periods": periods}
    except Exception:  # noqa: BLE001
        return None
