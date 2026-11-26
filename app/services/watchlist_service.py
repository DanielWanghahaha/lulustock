"""观察池：添加/移除/列表。"""
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.portfolio_service import normalize_symbol
from app.storage.models import WatchlistItem


async def list_watchlist(session: AsyncSession, user_id: int) -> list[WatchlistItem]:
    result = await session.execute(
        select(WatchlistItem).where(WatchlistItem.user_id == user_id).order_by(WatchlistItem.added_at)
    )
    return list(result.scalars().all())


async def add_watch(session: AsyncSession, user_id: int, raw_symbol: str, note: str | None = None,
                    high: Decimal | None = None, low: Decimal | None = None) -> tuple[WatchlistItem | None, str | None]:
    symbol = normalize_symbol(raw_symbol)
    if symbol is None:
        return None, f"股票代码无效：{raw_symbol}"
    existing = (
        await session.execute(
            select(WatchlistItem).where(WatchlistItem.user_id == user_id, WatchlistItem.symbol == symbol)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None, f"{symbol} 已在观察池中"
    item = WatchlistItem(user_id=user_id, symbol=symbol, note=note, price_alert_high=high, price_alert_low=low)
    session.add(item)
    await session.flush()
    return item, None


async def remove_watch(session: AsyncSession, user_id: int, raw_symbol: str) -> tuple[bool, str | None]:
    symbol = normalize_symbol(raw_symbol)
    if symbol is None:
        return False, f"股票代码无效：{raw_symbol}"
    result = await session.execute(
        delete(WatchlistItem).where(WatchlistItem.user_id == user_id, WatchlistItem.symbol == symbol)
    )
    if result.rowcount == 0:
        return False, f"{symbol} 不在观察池中"
    return True, None


def parse_alerts(text: str) -> tuple[Decimal | None, Decimal | None, str | None]:
    """从描述解析 上限X 下限Y；返回 (high, low, error)。"""
    high = low = None
    words = text.split()
    cleaned: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        try:
            if w.startswith("上限"):
                high = Decimal(w[2:])
            elif w.startswith("下限"):
                low = Decimal(w[2:])
            else:
                cleaned.append(w)
        except (InvalidOperation, IndexError):
            return None, None, f"价格阈值无效：{w}"
        i += 1
    return high, low, None
