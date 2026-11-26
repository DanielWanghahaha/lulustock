"""M0 基础命令：/start /menu /help /status /ping。"""
import logging
import time

from sqlalchemy import text
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config import get_settings
from app.storage.db import Session

log = logging.getLogger(__name__)


def _authorized(update: Update) -> bool:
    """仅允许白名单用户在私聊中使用。"""
    settings = get_settings()
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None or chat.type != "private":
        return False
    allowed = settings.allowed_user_ids
    if not allowed or user.id not in allowed:
        return False
    return True


async def _deny(update: Update) -> None:
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return  # 群聊中静默忽略
    if update.message:
        await update.message.reply_text("⛔ 未授权访问。请联系管理员将你的 Telegram 用户 ID 加入白名单。")


def _main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📊 我的持仓"), KeyboardButton("📈 今日简报")],
        [KeyboardButton("🔍 个股研究"), KeyboardButton("⭐ 投资观察池")],
        [KeyboardButton("⚠️ 风险预警"), KeyboardButton("📄 投资报告")],
        [KeyboardButton("✏️ 交易入账"), KeyboardButton("⚙️ 设置 / 状态")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    await update.message.reply_text(
        "👋 欢迎使用 **投资管家**\n\n"
        "我是你的 A 股中长期投资研究与持仓管理助手。\n\n"
        "**我能做：**\n"
        "• 记录并管理你的持仓与交易\n"
        "• 每日盘后简报与风险预警\n"
        "• 个股研究与三情景估值\n"
        "• 组合风险诊断（集中度、回撤）\n\n"
        "**我不能做：**\n"
        "• 不自动交易、不荐股、不保证收益\n\n"
        "点击下方菜单或直接输入命令开始。",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    await update.message.reply_text("🏠 投资管家 | 主菜单", reply_markup=_main_menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    await update.message.reply_text(
        "📖 **命令列表**\n\n"
        "/start — 初始化与欢迎\n"
        "/menu — 主菜单\n"
        "/help — 本帮助\n"
        "/status — 数据库与系统状态\n"
        "/ping — 数据库延迟测试\n\n"
        "更多功能将随模块上线逐步开放。",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    msg = await update.message.reply_text("⏳ 正在检查系统状态…")
    lines = ["🖥 **系统状态**\n"]
    try:
        async with Session() as session:
            t0 = time.perf_counter()
            result = await session.execute(text("SELECT VERSION()"))
            version = result.scalar()
            db_ms = (time.perf_counter() - t0) * 1000
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = 'stock_agent'"
                )
            )
            table_count = result.scalar()
        lines.append(f"✅ 数据库：MySQL {version}（{db_ms:.0f} ms）")
        lines.append(f"✅ 数据库表数量：{table_count}")
    except Exception as exc:  # noqa: BLE001
        log.exception("DB status check failed")
        lines.append(f"❌ 数据库异常：{type(exc).__name__}: {exc}")
    settings = get_settings()
    lines.append(f"🌐 运行环境：{settings.app_env}")
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    t0 = time.perf_counter()
    try:
        async with Session() as session:
            await session.execute(text("SELECT 1"))
        ms = (time.perf_counter() - t0) * 1000
        await update.message.reply_text(f"🏓 Pong！数据库延迟 {ms:.0f} ms")
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"❌ 数据库不可达：{exc}")
