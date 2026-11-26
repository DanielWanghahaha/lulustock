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
    """首次调用时拉取全 A 股列表缓存（新浪 hs_a，约 5800 行）。"""
    count = (await session.execute(select(func.count()).select_from(Security))).scalar()
    if count and count > 5000:
        return count
    total_inserted = 0
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
        page = 1
        while page <= 80:
            resp = await client.get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                params={"page": str(page), "num": "100", "sort": "symbol", "asc": "1", "node": "hs_a"},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for d in rows:
                raw = (d.get("symbol") or "").strip().lower()  # 如 sh600036 / sz000001 / bj920000
                name = (d.get("name") or "").strip()
                if not raw or not name:
                    continue
                prefix, _, code = raw.partition(":")
                market = None
                if raw.startswith("sh"):
                    market, code = "SH", raw[2:]
                elif raw.startswith("sz"):
                    market, code = "SZ", raw[2:]
                elif raw.startswith("bj"):
                    market, code = "BJ", raw[2:]
                if market is None or not (code.isdigit() and len(code) == 6):
                    continue
                symbol = f"{code}.{market}"
                stmt = mysql_insert(Security).values(
                    symbol=symbol, name=name, market=market,
                    updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                stmt = stmt.on_duplicate_key_update(name=stmt.inserted.name)
                await session.execute(stmt)
                total_inserted += 1
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


async def names_for(session: AsyncSession, symbols: list[str]) -> dict[str, str]:
    """批量查询代码→名称：本地缓存 → 腾讯批量接口补齐（并回填缓存）。"""
    if not symbols:
        return {}
    rows = (
        await session.execute(select(Security).where(Security.symbol.in_(symbols)))
    ).scalars().all()
    result = {r.symbol: r.name for r in rows}
    missing = [s for s in symbols if s not in result]
    if missing:
        try:
            fetched = await fetch_names_tencent(missing)
            result.update(fetched)
            for sym, nm in fetched.items():
                code, _, suffix = sym.partition(".")
                stmt = mysql_insert(Security).values(
                    symbol=sym, name=nm, market=suffix,
                    updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                stmt = stmt.on_duplicate_key_update(name=stmt.inserted.name)
                await session.execute(stmt)
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            log.warning("tencent name fetch failed: %s", exc)
    return result


async def fetch_names_tencent(symbols: list[str]) -> dict[str, str]:
    """腾讯批量行情取名称。"""
    codes = []
    for s in symbols:
        code, _, suffix = s.partition(".")
        if suffix in ("SH", "SZ"):
            codes.append(f"{suffix.lower()}{code}")
    if not codes:
        return {}
    out: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
        for i in range(0, len(codes), 40):
            batch = codes[i : i + 40]
            resp = await client.get("https://qt.gtimg.cn/q=" + ",".join(batch))
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="replace")
            for entry in text.split(";"):
                entry = entry.strip()
                if "=" not in entry:
                    continue
                var, body = entry.split("=", 1)
                fields = body.strip('"').split("~")
                if len(fields) < 45 or not fields[2]:
                    continue
                code, suffix = fields[2], var[-2:].lower().replace("v_", "")
                # var 形如 v_sh600036
                v = var.replace("v_", "")
                prefix, market = v[:2].lower(), v[:2].upper()
                sym = f"{code}.{market}"
                out[sym] = fields[1]
    return out
