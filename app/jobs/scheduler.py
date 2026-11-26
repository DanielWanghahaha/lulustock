"""后台任务：Outbox 发送器、盘后简报、周报、月报（含执行审计与用户开关）。"""
import logging
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.services import outbox_service, quotes_service, report_service
from app.services.risk_engine import evaluate_portfolio
from app.services.risk_service import record_findings
from app.services.settings_service import get_setting
from app.services.user_service import get_default_portfolio
from app.storage.db import Session
from app.storage.models import JobRun, User

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _record_run(name: str, ok: bool, detail: str = "") -> None:
    try:
        async with Session() as session:
            session.add(
                JobRun(
                    job_name=name,
                    status="ok" if ok else "error",
                    detail=detail[:500],
                    finished_at=_utcnow(),
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        log.exception("record_run failed")


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
    """交易日盘后：更新行情 → 快照 → 风险事件 → 简报入 Outbox（每用户每日一次）。"""
    if not get_settings().enable_schedules:
        return
    log.info("post-close job start")
    ok = True
    detail = ""
    try:
        async with Session() as session:
            users = (await session.execute(select(User))).scalars().all()
            for user in users:
                if not await get_setting(session, user.id, "auto_daily_brief"):
                    continue
                portfolio = await get_default_portfolio(session, user.id)
                result = await quotes_service.update_quotes(session)
                snap = await quotes_service.persist_snapshot(session, portfolio)
                await session.commit()
                findings, _summary = await evaluate_portfolio(session, user.id, portfolio.id, None)
                new_n = await record_findings(session, user.id, findings)
                await session.commit()
                brief = await report_service.build_daily_brief(session, user.id, portfolio)
                async with Session() as s2:
                    await outbox_service.enqueue(
                        s2,
                        user.id,
                        user.telegram_user_id,
                        f"daily_brief:{user.id}:{date.today().isoformat()}",
                        brief,
                    )
                    await s2.commit()
                detail += f"u{user.id}: quotes={result['ok']}/{result['failed']} nav={snap.nav} findings+{new_n}; "
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
        log.exception("post-close job failed")
    await _record_run("post_close_brief", ok, detail)
    log.info("post-close job done")


async def weekly_job(bot) -> None:
    """周六 10:00 生成上周周报并入 Outbox。"""
    if not get_settings().enable_schedules:
        return
    ok = True
    detail = ""
    try:
        async with Session() as session:
            users = (await session.execute(select(User))).scalars().all()
            for user in users:
                if not await get_setting(session, user.id, "auto_weekly"):
                    continue
                portfolio = await get_default_portfolio(session, user.id)
                content = await report_service.weekly_report(session, user.id, portfolio)
                async with Session() as s2:
                    await outbox_service.enqueue(
                        s2,
                        user.id,
                        user.telegram_user_id,
                        f"weekly_report:{user.id}:{date.today().isoformat()}",
                        content,
                    )
                    await s2.commit()
                detail += f"u{user.id}; "
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
        log.exception("weekly job failed")
    await _record_run("weekly_report", ok, detail)


async def monthly_job(bot) -> None:
    """每月 1 日 10:00 生成上月月报并入 Outbox。"""
    if not get_settings().enable_schedules:
        return
    ok = True
    detail = ""
    try:
        async with Session() as session:
            users = (await session.execute(select(User))).scalars().all()
            for user in users:
                if not await get_setting(session, user.id, "auto_monthly"):
                    continue
                portfolio = await get_default_portfolio(session, user.id)
                content = await report_service.monthly_report(session, user.id, portfolio)
                async with Session() as s2:
                    await outbox_service.enqueue(
                        s2,
                        user.id,
                        user.telegram_user_id,
                        f"monthly_report:{user.id}:{date.today().isoformat()}",
                        content,
                    )
                    await s2.commit()
                detail += f"u{user.id}; "
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
        log.exception("monthly job failed")
    await _record_run("monthly_report", ok, detail)
