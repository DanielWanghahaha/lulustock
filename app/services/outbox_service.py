"""Outbox：通知先落库，由发送器异步投递；重复事件按 event_key 去重。"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import OutboxMessage


async def enqueue(session: AsyncSession, user_id: int, chat_id: int, event_key: str, text: str) -> bool:
    """返回是否新建（False=重复事件被去重）。"""
    existing = (
        await session.execute(select(OutboxMessage.id).where(OutboxMessage.event_key == event_key))
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(OutboxMessage(user_id=user_id, chat_id=chat_id, event_key=event_key, text=text))
    await session.flush()
    return True


async def fetch_pending(session: AsyncSession, limit: int = 20) -> list[OutboxMessage]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await session.execute(
        select(OutboxMessage)
        .where(OutboxMessage.status == "pending")
        .order_by(OutboxMessage.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())
