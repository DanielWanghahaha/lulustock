"""Bot 入口：注册命令并启动 long polling。"""
import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bot.handlers import (
    cancel_cmd,
    help_cmd,
    import_cmd,
    menu,
    on_callback,
    on_document,
    on_text,
    ping,
    portfolio_cmd,
    start,
    status,
    trade_cmd,
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

    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^act:"))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
