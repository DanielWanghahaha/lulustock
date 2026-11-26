"""证券搜索：本地缓存优先（代码/名称），回退东财拼音建议接口。"""
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.portfolio_service import normalize_symbol
from app.storage.models import Security

log = logging.getLogger(__name__)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
_MARKET_MAP = {"1": ".SH", "2": ".SZ"}


async def ensure_securities(session: AsyncSession) -> int:
    """首次调用时拉取全 A 股列表缓存（约 5500 行）。"""
    count = (await session.execute(select(func.count()).select_from(Security))).scalar()
    if count and count > 5000:
        return count
    total_inserted = 0
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
        page = 1
        while page <= 30:
            resp = await client.get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                params={
                    "pn": str(page), "pz": "500", "po": "1", "np": "1",
                    "fltt": "2", "invt": "2", "fid": "f12",
                    "fs": "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80",
                    "fields": "f12,f14",
                },
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            rows = data.get("diff") or []
            if not rows:
                break
            for d in rows:
                code, name = d.get("f12"), d.get("f14")
                if not code or not name:
                    continue
                symbol = normalize_symbol(code)
                if symbol is None:
                    continue
                market = "SH" if symbol.endswith(".SH") else ("SZ" if symbol.endswith(".SZ") else "BJ")
                stmt = mysql_insert(Security).values(
                    symbol=symbol, name=name, market=market,
                    updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                stmt = stmt.on_duplicate_key_update(name=stmt.inserted.name)
                await session.execute(stmt)
                total_inserted += 1
            if len(rows) < 500:
                break
            page += 1
    await session.flush()
    log.info("securities cache loaded: %s", total_inserted)
    return total_inserted


async def _search_local(session: AsyncSession, query: str, limit: int = 8) -> list[dict[str, str]]:
    q = query.strip()
    if not q:
        return []
    results: list[dict[str, str]] = []
    if q.isdigit():
        rows = (
            await session.execute(
                select(Security).where(Security.symbol.like(f"{q[:6]}%")).limit(limit)
            )
        ).scalars().all()
        results = [{"symbol": r.symbol, "name": r.name} for r in rows]
    if not results:
        rows = (
            await session.execute(
                select(Security).where(Security.name.like(f"%{q}%")).limit(limit)
            )
        ).scalars().all()
        results = [{"symbol": r.symbol, "name": r.name} for r in rows]
    return results


async def _search_suggest(query: str, limit: int = 8) -> list[dict[str, str]]:
    """东财建议接口：支持汉字与拼音。"""
    out: list[dict[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=8, headers=_HEADERS) as client:
            resp = await client.get(
                "https://searchapi.eastmoney.com/api/suggest/get",
                params={
                    "input": query, "type": "14",
                    "token": "D43B7D6ABF04CE49F5E5AA8DDEDAE1CD", "count": str(limit * 2),
                },
            )
            resp.raise_for_status()
            rows = ((resp.json().get("QuotationCodeTable") or {}).get("Data")) or []
    except Exception:  # noqa: BLE001
        return out
    for d in rows:
        mt = str(d.get("MarketType"))
        suffix = _MARKET_MAP.get(mt)
        code = d.get("Code") or ""
        if suffix and code.isdigit() and len(code) == 6:
            symbol = code + suffix
            if symbol not in {x["symbol"] for x in out}:
                out.append({"symbol": symbol, "name": d.get("Name") or ""})
        if len(out) >= limit:
            break
    return out


async def search_securities(session: AsyncSession, query: str, limit: int = 8) -> list[dict[str, str]]:
    """搜索入口：本地代码/名称 → 拼音建议。"""
    try:
        await ensure_securities(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("securities cache load failed: %s", exc)
    results = await _search_local(session, query, limit)
    if not results:
        results = await _search_suggest(query, limit)
    return results
