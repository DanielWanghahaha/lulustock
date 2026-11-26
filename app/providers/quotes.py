"""日线行情 Provider：默认腾讯公开 K 线接口，可替换 Tushare 等授权源。"""
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol

import httpx

from app.services.portfolio_service import normalize_symbol

_TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}


@dataclass
class Quote:
    symbol: str
    trade_date: date
    close: float
    source: str
    fetched_at: datetime
    quality: str = "ok"
    source_url: str | None = None


class QuotesProvider(Protocol):
    async def get_daily_closes(self, symbol: str, begin: date, end: date) -> list[Quote]: ...


def _tencent_code(symbol: str) -> str | None:
    code, _, suffix = symbol.partition(".")
    if suffix in ("SH", "SZ"):
        return f"{suffix.lower()}{code}"
    return None  # BJ 暂不支持


class TencentProvider:
    """腾讯公开日 K 接口（未复权收盘）。个人研究用途，生产建议替换为授权数据源。"""

    source = "tencent"

    async def get_daily_closes(self, symbol: str, begin: date, end: date) -> list[Quote]:
        code = _tencent_code(symbol)
        if code is None:
            return []
        count = 320
        url = f"{_TENCENT_KLINE}?param={code},day,,,{count}"
        async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        node = (payload.get("data") or {}).get(code) or {}
        rows = node.get("day") or []
        quotes: list[Quote] = []
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for row in rows:
            try:
                d = date.fromisoformat(row[0])
                if begin <= d <= end:
                    quotes.append(
                        Quote(
                            symbol=symbol,
                            trade_date=d,
                            close=float(row[2]),
                            source=self.source,
                            fetched_at=now,
                            source_url=url,
                        )
                    )
            except (ValueError, IndexError):
                continue
        return quotes


async def fetch_closes_with_retry(
    provider: QuotesProvider, symbol: str, begin: date, end: date, retries: int = 2
) -> list[Quote]:
    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            return await provider.get_daily_closes(symbol, begin, end)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            await asyncio.sleep(1)
    raise RuntimeError(f"行情获取失败 {symbol}: {last_exc}")
