"""Bot 入口：注册命令并启动 long polling。"""
import logging
from telegram import Update
from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

BOT_COMMANDS = [
    BotCommand("today", "今日盘后简报"),
    BotCommand("portfolio", "查看我的持仓"),
    BotCommand("import", "导入持仓 CSV"),
    BotCommand("trade", "登记已执行交易"),
    BotCommand("research", "个股研究（如 /research 600036.SH）"),
    BotCommand("valuation", "三情景估值"),
    BotCommand("watchlist", "投资观察池与价格触发"),
    BotCommand("alerts", "风险预警历史"),
    BotCommand("report", "投资周报/月报"),
    BotCommand("settings", "自动任务开关"),
    BotCommand("status", "系统与数据状态"),
    BotCommand("menu", "主菜单"),
    BotCommand("help", "帮助与能力边界"),
]

from app.bot.handlers import (
    alerts_cmd,
    cancel_cmd,
    decision_cmd,
    stress_cmd,
    help_cmd,
    import_cmd,
    menu,
    on_callback,
    on_document,
    on_dec_callback,
    on_rs_callback,
    on_set_callback,
    on_text,
    on_wladd_callback,
    ping,
    portfolio_cmd,
    report_cmd,
    research_cmd,
    settings_cmd,
    start,
    status,
    trade_cmd,
    today_cmd,
    update_quotes_cmd,
    valuation_cmd,
    watchlist_cmd,
)
from app.config import get_settings


def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 未配置，请在 .env 中填写")
    if not settings.allowed_user_ids:
        raise SystemExit(
            "TELEGRAM_ALLOWED_USER_IDS 未配置，请在 .env 中填写你的 Telegram 用户 ID"
        )

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=settings.log_level,
    )

    async def _post_init(application) -> None:  # type: ignore[no-untyped-def]
        await application.bot.set_my_commands(BOT_COMMANDS, language_code="zh")
        await application.bot.set_my_commands(BOT_COMMANDS)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("update_quotes", update_quotes_cmd))
    app.add_handler(CommandHandler("research", research_cmd))
    app.add_handler(CommandHandler("valuation", valuation_cmd))
    app.add_handler(CommandHandler("alerts", alerts_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("stress", stress_cmd))
    app.add_handler(CommandHandler("decision", decision_cmd))
    from app.bot.trade_wizard import build_trade_conversation

    app.add_handler(build_trade_conversation())
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^act:"))
    app.add_handler(CallbackQueryHandler(on_wladd_callback, pattern=r"^wladd:"))
    app.add_handler(CallbackQueryHandler(on_set_callback, pattern=r"^set:"))
    app.add_handler(CallbackQueryHandler(on_rs_callback, pattern=r"^rsm:"))
    app.add_handler(CallbackQueryHandler(on_dec_callback, pattern=r"^dec:"))

    async def on_error(update: object, context) -> None:  # type: ignore[no-untyped-def]
        logging.getLogger(__name__).error("Unhandled error", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ 处理出错，请稍后重试；若持续出现请联系管理员。",
                )
            except Exception:  # noqa: BLE001
                pass

    app.add_error_handler(on_error)

    from app.jobs.scheduler import monthly_job, post_close_job, send_pending_outbox, weekly_job

    async def _outbox_job(context) -> None:  # type: ignore[no-untyped-def]
        await send_pending_outbox(context.bot)

    async def _post_close(context) -> None:  # type: ignore[no-untyped-def]
        await post_close_job(context.bot)

    async def _weekly(context) -> None:  # type: ignore[no-untyped-def]
        await weekly_job(context.bot)

    async def _monthly(context) -> None:  # type: ignore[no-untyped-def]
        await monthly_job(context.bot)

    if settings.enable_schedules:
        from zoneinfo import ZoneInfo

        import datetime as dtmod

        app.job_queue.run_repeating(_outbox_job, interval=10, first=3, name="outbox_sender")
        app.job_queue.run_daily(
            _post_close,
            time=dtmod.time(hour=16, minute=35, tzinfo=ZoneInfo("Asia/Shanghai")),
            days=(0, 1, 2, 3, 4),
            name="post_close_brief",
        )
        app.job_queue.run_daily(
            _weekly,
            time=dtmod.time(hour=10, minute=0, tzinfo=ZoneInfo("Asia/Shanghai")),
            days=(5,),
            name="weekly_report",
        )
        app.job_queue.run_monthly(
            _monthly,
            when=dtmod.time(hour=10, minute=0, tzinfo=ZoneInfo("Asia/Shanghai")),
            day=1,
            name="monthly_report",
        )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
