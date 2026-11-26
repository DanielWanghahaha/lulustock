"""主动筛选：预设候选池 + 透明评分（分数仅代表研究优先级，不构成买入信号）。"""
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from app.providers.fundamentals import fetch_financial_summary
from app.services.market_context import gather_price_context
from app.storage.db import Session

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

POOLS: dict[str, dict[str, Any]] = {
    "dividend": {
        "label": "红利候选池",
        "symbols": [
            "601398.SH", "601939.SH", "601288.SH", "601988.SH", "601328.SH",
            "600036.SH", "600900.SH", "601088.SH", "601225.SH", "601006.SH",
            "600028.SH", "601628.SH", "600030.SH", "601088.SH", "600188.SH",
        ],
    },
    "value": {
        "label": "优质价值候选池",
        "symbols": [
            "600519.SH", "000858.SZ", "000651.SZ", "000333.SZ", "600585.SH",
            "000002.SZ", "600048.SH", "600276.SH", "000538.SZ", "600660.SH",
            "601318.SH", "600887.SH", "000596.SZ", "002304.SZ", "600809.SH",
        ],
    },
    "cyclical": {
        "label": "行业周期候选池",
        "symbols": [
            "601088.SH", "601225.SH", "601899.SH", "600019.SH", "600585.SH",
            "600309.SH", "601857.SH", "601600.SH", "600111.SH", "002714.SZ",
            "600028.SH", "601168.SH",
        ],
    },
}


async def fetch_batch_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """腾讯批量快照：现价/PE/PB/总市值。"""
    codes = []
    for s in symbols:
        code, _, suffix = s.partition(".")
        if suffix in ("SH", "SZ"):
            codes.append((s, f"{suffix.lower()}{code}"))
    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
        for i in range(0, len(codes), 40):
            batch = codes[i : i + 40]
            resp = await client.get("https://qt.gtimg.cn/q=" + ",".join(c for _, c in batch))
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="replace")
            for entry in text.split(";"):
                entry = entry.strip()
                if "=" not in entry or "~" not in entry:
                    continue
                var, body = entry.split("=", 1)
                f = body.strip('"').split("~")
                if len(f) < 47:
                    continue
                sym_code = var.replace("v_", "")
                market = sym_code[:2].upper()
                symbol = f"{sym_code[2:]}.{market}"
                def _num(idx: str) -> float | None:
                    try:
                        v = float(f[int(idx)])
                        return v if v > 0 else None
                    except (ValueError, IndexError):
                        return None
                out[symbol] = {
                    "name": f[1],
                    "price": _num("3"),
                    "pe_ttm": _num("39"),
                    "total_market_cap_yi": _num("45"),
                    "pb": _num("46"),
                }
    return out


def _score_dividend(pe, pb, profit_yoy, roe) -> tuple[float, dict]:
    pb_s = max(0.0, 35 * (1 - (pb or 2) / 1.5)) if pb is not None else 0
    pe_s = max(0.0, 25 * (1 - (pe or 12) / 12)) if pe is not None else 0
    if profit_yoy is None:
        g_s = 0.0
    elif profit_yoy > 0:
        g_s = 20.0
    elif profit_yoy > -5:
        g_s = 10.0
    else:
        g_s = 0.0
    roe_s = 20 * min(roe or 0, 15) / 15
    return round(pb_s + pe_s + g_s + roe_s, 1), {"PB分": round(pb_s), "PE分": round(pe_s), "盈利分": round(g_s), "ROE分": round(roe_s)}


def _score_value(pe, rev_yoy, profit_yoy, roe) -> tuple[float, dict]:
    pe_s = max(0.0, 40 * (1 - (pe or 20) / 20)) if pe is not None else 0
    if rev_yoy is None:
        g_s = 0.0
    elif rev_yoy > 5:
        g_s = 30.0
    elif rev_yoy > 0:
        g_s = 15.0
    else:
        g_s = 0.0
    if profit_yoy is not None and profit_yoy > 0:
        g_s += 5.0
    roe_s = 30 * min(roe or 0, 20) / 20
    return round(pe_s + g_s + roe_s, 1), {"PE分": round(pe_s), "增长分": round(g_s), "ROE分": round(roe_s)}


async def _score_cyclical(symbol: str, price: float | None, profit_yoy) -> tuple[float, dict]:
    async with Session() as session:
        ctx = await gather_price_context(session, symbol, days=420)
    if not ctx or ctx.get("high_250d") is None:
        return 0.0, {}
    low, high = ctx["low_250d"], ctx["high_250d"]
    pos_in_range = 0.5
    if high > low and price:
        pos_in_range = max(0.0, min(1.0, (price - low) / (high - low)))
    pos_s = 60 * (1 - pos_in_range)  # 区间低位高分
    if profit_yoy is None:
        g_s = 0.0
    elif profit_yoy > 0:
        g_s = 40.0
    elif profit_yoy > -10:
        g_s = 20.0
    else:
        g_s = 0.0
    return round(pos_s + g_s, 1), {"位置分": round(pos_s), "盈利改善分": round(g_s)}


@dataclass
class ScreenRow:
    symbol: str
    name: str
    score: float
    pe: float | None
    pb: float | None
    profit_yoy: float | None
    components: dict


async def run_screening(pool_key: str, limit: int = 15) -> tuple[str, list[ScreenRow]]:
    pool = POOLS[pool_key]
    quotes = await fetch_batch_quotes(pool["symbols"][:limit])
    rows: list[ScreenRow] = []
    for symbol in pool["symbols"][:limit]:
        q = quotes.get(symbol)
        if not q:
            continue
        try:
            fin = await fetch_financial_summary(symbol, periods=1)
        except Exception:  # noqa: BLE001
            fin = []
        latest = fin[0] if fin else {}
        pe, pb = q.get("pe_ttm"), q.get("pb")
        profit_yoy = latest.get("net_profit_yoy_pct")
        roe = latest.get("weighted_roe_pct")
        rev_yoy = latest.get("revenue_yoy_pct")
        price = q.get("price")
        if pool_key == "dividend":
            score, comp = _score_dividend(pe, pb, profit_yoy, roe)
        elif pool_key == "value":
            score, comp = _score_value(pe, rev_yoy, profit_yoy, roe)
        else:
            score, comp = await _score_cyclical(symbol, price, profit_yoy)
        rows.append(
            ScreenRow(
                symbol=symbol, name=q.get("name") or symbol, score=score,
                pe=pe, pb=pb, profit_yoy=profit_yoy, components=comp,
            )
        )
    rows.sort(key=lambda r: r.score, reverse=True)
    return pool["label"], rows


def score_tier(score: float) -> str:
    if score >= 85:
        return "🥇 优先研究"
    if score >= 75:
        return "🥈 重点观察"
    if score >= 60:
        return "🥉 储备"
    return "观察"
