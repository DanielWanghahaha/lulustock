"""风险事件持久化与查询。"""
import hashlib
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.risk_engine import Finding
from app.storage.models import RiskEvent


async def record_findings(session: AsyncSession, user_id: int, findings: list[Finding]) -> int:
    """按 (用户, 规则, 内容, 日期) 去重入库；返回新增条数。"""
    today = date.today().isoformat()
    created = 0
    for f in findings:
        digest = hashlib.md5(f.message.encode("utf-8")).hexdigest()[:12]
        key = f"{user_id}:{f.code}:{digest}:{today}"
        exists = (
            await session.execute(select(RiskEvent.id).where(RiskEvent.dedupe_key == key))
        ).scalar_one_or_none()
        if exists is not None:
            continue
        session.add(
            RiskEvent(user_id=user_id, level=f.level, code=f.code, message=f.message, dedupe_key=key)
        )
        created += 1
    await session.flush()
    return created


async def list_events(session: AsyncSession, user_id: int, limit: int = 20) -> list[RiskEvent]:
    result = await session.execute(
        select(RiskEvent)
        .where(RiskEvent.user_id == user_id)
        .order_by(RiskEvent.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
