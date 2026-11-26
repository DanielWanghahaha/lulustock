"""后台任务：Outbox 发送器、盘后行情更新与简报。"""
import logging
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.services import outbox_service, quotes_service, report_service
from app.services.user_service import get_default_portfolio
from app.storage.db import Session
from app.storage.models import User

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def send_pending_outbox(bot) -> None:
    """把待发送通知投递到 Telegram；失败保留重试。"""
    async with Session() as session:
        pending = await outbox_service.fetch_pending(session)
        if not pending:
            return
        for msg in pending:
            try:
                await bot.send_message(chat_id=msg.chat_id, text=msg.text, parse_mode="Markdown")
                msg.status = "sent"
                msg.sent_at = _utcnow()
            except Exception as exc:  # noqa: BLE001
                log.warning("outbox send failed id=%s: %s", msg.id, exc)
                msg.attempts += 1
                if msg.attempts >= 5:
                    msg.status = "failed"
        await session.commit()


async def post_close_job(bot) -> None:
    """交易日盘后：更新行情 → 快照 → 简报入 Outbox（每用户每日一次，按 event_key 去重）。"""
    settings = get_settings()
    if not settings.enable_schedules:
        return
    log.info("post-close job start")
    async with Session() as session:
        users = (await session.execute(select(User))).scalars().all()
        for user in users:
            portfolio = await get_default_portfolio(session, user.id)
            result = await quotes_service.update_quotes(session)
            await persist_snapshot(session, portfolio)
            await session.commit()
            brief = await report_service.build_daily_brief(session, user.id, portfolio)
            async with Session() as session2:
                await outbox_service.enqueue(
                    session2,
                    user.id,
                    user.telegram_user_id,
                    f"daily_brief:{user.id}:{date.today().isoformat()}",
                    brief,
                )
                await session2.commit()
    log.info("post-close job done")
