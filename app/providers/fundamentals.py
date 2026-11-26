"""基本面数据：东方财富公开快照（PE/PB/市值/ROE）与 F10 主要财务指标。"""
from typing import Any

import httpx

from app.services.portfolio_service import normalize_symbol

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://emweb.securities.eastmoney.com/",
}


def _secid(symbol: str) -> str | None:
    code, _, suffix = symbol.partition(".")
    if suffix == "SH":
        return f"1.{code}"
    if suffix == "SZ":
        return f"0.{code}"
    return None


def _emcode(symbol: str) -> str | None:
    code, _, suffix = symbol.partition(".")
    if suffix in ("SH", "SZ"):
        return f"{suffix}{code}"
    return None


async def fetch_valuation_snapshot(symbol: str) -> dict[str, Any] | None:
    """实时估值快照：名称/现价/PE(TTM)/PB/总市值/ROE。"""
    secid = _secid(symbol)
    if secid is None:
        return None
    async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
        resp = await client.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": secid, "fields": "f43,f57,f58,f116,f162,f167,f173"},
        )
        resp.raise_for_status()
        data = (resp.json().get("data") or {})
    if not data:
        return None

    def _scale(v, factor: float) -> float | None:
        return v / factor if isinstance(v, (int, float)) and v > 0 else None

    return {
        "name": data.get("f58"),
        "price": _scale(data.get("f43"), 100),
        "pe_ttm": _scale(data.get("f162"), 100),
        "pb": _scale(data.get("f167"), 100),
        "total_market_cap_yi": round(data["f116"] / 1e8, 1) if isinstance(data.get("f116"), (int, float)) else None,
        "roe_pct": data.get("f173") if isinstance(data.get("f173"), (int, float)) else None,
        "source": "eastmoney_snapshot",
    }


_SUMMARY_FIELDS = {
    "REPORT_DATE": "report_date",
    "TOTALOPERATEREVE": "revenue",
    "TOTALOPERATEREVETZ": "revenue_yoy_pct",
    "PARENTNETPROFIT": "net_profit",
    "PARENTNETPROFITTZ": "net_profit_yoy_pct",
    "ROEJQ": "weighted_roe_pct",
    "XSMLL": "gross_margin_pct",
    "ZCFZL": "debt_ratio_pct",
    "MGJYXJJE": "ocf_per_share",
    "EPSJB": "eps_basic",
}


async def fetch_financial_summary(symbol: str, periods: int = 6) -> list[dict[str, Any]]:
    """F10 主要财务指标（按报告期倒序）。"""
    code = _emcode(symbol)
    if code is None:
        return []
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
        resp = await client.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew",
            params={"type": "0", "code": code},
        )
        resp.raise_for_status()
        rows = resp.json().get("data") or []
    out: list[dict[str, Any]] = []
    for row in rows[:periods]:
        item: dict[str, Any] = {}
        for src, dst in _SUMMARY_FIELDS.items():
            v = row.get(src)
            if dst == "report_date" and isinstance(v, str):
                item[dst] = v[:10]
            elif isinstance(v, (int, float)):
                if dst in ("revenue", "net_profit"):
                    item[dst] = round(v / 1e8, 2)  # 亿元
                else:
                    item[dst] = round(float(v), 2)
            else:
                item[dst] = None
        out.append(item)
    return out
