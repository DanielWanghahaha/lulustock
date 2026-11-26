"""Bot 命令与消息处理：M0 基础命令 + M1 持仓与交易。"""
import logging
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from app.config import get_settings
from app.services.ledger_service import TradePreview, confirm_trade, parse_trade_text
from app.services.portfolio_service import (
    confirm_import,
    content_hash,
    parse_import_csv,
    portfolio_snapshot,
)
from app.services.user_service import ensure_policy_v1, ensure_user, get_default_portfolio
from app.storage.db import Session
from app.storage.models import (
    PendingAction,
    PendingActionType,
    PendingStatus,
    TradeDirection,
    User,
)

log = logging.getLogger(__name__)

PENDING_TTL_MINUTES = 30
DIR_LABELS = {
    TradeDirection.buy: "买入",
    TradeDirection.sell: "卖出",
    TradeDirection.deposit: "入金",
    TradeDirection.withdraw: "出金",
    TradeDirection.dividend: "分红到账",
    TradeDirection.seed: "初始建仓",
}


def _utcnow() -> datetime:
    """数据库 DATETIME 统一存取 naive UTC。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _authorized(update: Update) -> bool:
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
        return
    if update.message:
        await update.message.reply_text("⛔ 未授权访问。请联系管理员将你的 Telegram 用户 ID 加入白名单。")


def _main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📊 我的持仓"), KeyboardButton("📥 导入持仓")],
        [KeyboardButton("✏️ 交易入账"), KeyboardButton("🖥 系统状态")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def _load_user(update: Update):
    """读取已绑定用户与默认组合；未绑定时提示 /start。"""
    tg_id = update.effective_user.id  # type: ignore[union-attr]
    async with Session() as session:
        result = await session.execute(select(User).where(User.telegram_user_id == tg_id))
        user = result.scalar_one_or_none()
        if user is None:
            if update.message:
                await update.message.reply_text("请先发送 /start 完成初始化。")
            return None
        portfolio = await get_default_portfolio(session, user.id)
        return user, portfolio


def _fmt(d, places: str = "0.01") -> str:
    if d is None:
        return "—"
    return f"{d.quantize(Decimal(places)):,}"


async def _create_pending(user: User, chat_id: int, action_type: PendingActionType, payload: dict) -> PendingAction:
    async with Session() as session:
        pa = PendingAction(
            token=secrets.token_hex(4).upper(),
            user_id=user.id,
            chat_id=chat_id,
            action_type=action_type,
            payload=payload,
            expires_at=_utcnow() + timedelta(minutes=PENDING_TTL_MINUTES),
        )
        session.add(pa)
        await session.commit()
        await session.refresh(pa)
        return pa


def _confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ 确认入账", callback_data=f"act:confirm:{token}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"act:cancel:{token}"),
        ]]
    )


# ---------------------------------------------------------------- M0 命令


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None and update.effective_user is not None
    async with Session() as session:
        user = await ensure_user(session, update.effective_user.id, update.effective_user.full_name)
        portfolio = await get_default_portfolio(session, user.id)
        created_policy = await ensure_policy_v1(session, user.id)
        await session.commit()
    policy_note = "已创建初始策略配置 v1（proposed，待确认）" if created_policy else "策略配置已存在"
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
        f"✅ 已绑定账号（组合：{portfolio.name}）\n"
        f"📄 {policy_note}\n\n"
        "下一步：发送 /import 导入现有持仓，或 /trade 登记交易。",
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
        "/import — 导入持仓 CSV\n"
        "/trade — 登记已执行交易\n"
        "/portfolio — 查看持仓\n"
        "/cancel — 取消待确认操作\n"
        "/status — 数据库与系统状态\n"
        "/ping — 数据库延迟测试\n\n"
        "**交易示例：**\n"
        "• `买入 600036.SH 100 32.50 费用5`\n"
        "• `卖出 000001.SZ 200 11.80`\n"
        "• `入金 100000`\n"
        "• `分红 600036.SH 840`",
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
            version = (await session.execute(text("SELECT VERSION()"))).scalar()
            db_ms = (time.perf_counter() - t0) * 1000
            table_count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'stock_agent'")
                )
            ).scalar()
            pending = (
                await session.execute(
                    text("SELECT COUNT(*) FROM pending_actions WHERE status = 'pending'")
                )
            ).scalar()
        lines.append(f"✅ 数据库：MySQL {version}（{db_ms:.0f} ms）")
        lines.append(f"✅ 数据表：{table_count} 张")
        lines.append(f"⏳ 待确认操作：{pending} 个")
    except Exception as exc:  # noqa: BLE001
        log.exception("DB status check failed")
        lines.append(f"❌ 数据库异常：{type(exc).__name__}: {exc}")
    lines.append(f"🌐 运行环境：{get_settings().app_env}")
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


# ---------------------------------------------------------------- M1：导入

CSV_TEMPLATE = (
    "symbol,quantity,cost_per_share,currency,as_of\n"
    "600036.SH,100,32.00,CNY,2026-11-26\n"
    "000001.SZ,200,11.50,CNY,2026-11-26\n"
)


async def import_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    context.user_data["awaiting_import"] = True  # type: ignore[index]
    await update.message.reply_text(
        "📥 **导入持仓**\n\n"
        "请上传 CSV 文件（UTF-8 编码），格式如下：\n"
        f"```\n{CSV_TEMPLATE}```\n"
        "**列说明：**\n"
        "• symbol：股票代码（如 600036.SH）\n"
        "• quantity：持仓数量\n"
        "• cost_per_share：每股成本价\n"
        "• currency：币种（当前仅 CNY）\n"
        "• as_of：持仓日期（YYYY-MM-DD）\n\n"
        "⚠️ 每个代码只能出现一次；单次最多 100 行。\n"
        "发送 CSV 文件后我会解析并展示预览，确认后才写入。",
        parse_mode="Markdown",
    )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None and update.message.document is not None
    if not context.user_data.get("awaiting_import"):  # type: ignore[attr-defined]
        await update.message.reply_text("请先发送 /import 开始导入流程。")
        return
    doc = update.message.document
    if not (doc.file_name or "").lower().endswith(".csv"):
        await update.message.reply_text("⚠️ 请上传 .csv 文件。")
        return
    tg_file = await context.bot.get_file(doc.file_id)
    data = await tg_file.download_as_bytearray()
    try:
        content = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = bytes(data).decode("gbk")
        except UnicodeDecodeError:
            await update.message.reply_text("⚠️ 文件编码无法识别，请使用 UTF-8 或 GBK 保存 CSV。")
            return
    rows, errors = parse_import_csv(content)
    loaded = await _load_user(update)
    if loaded is None:
        return
    user, _portfolio = loaded
    if errors:
        await update.message.reply_text(
            "❌ **导入校验未通过：**\n" + "\n".join(f"• {e}" for e in errors[:20])
            + "\n\n修正后重新上传文件。",
            parse_mode="Markdown",
        )
        return
    if not rows:
        await update.message.reply_text("❌ CSV 中没有有效数据行。")
        return
    fhash = content_hash(content)
    payload = {
        "rows": [
            {
                "row_no": r.row_no,
                "symbol": r.symbol,
                "quantity": str(r.quantity),
                "cost_per_share": str(r.cost_per_share),
                "fee": str(r.fee),
                "currency": r.currency,
                "as_of": r.as_of.isoformat(),
            }
            for r in rows
        ],
        "file_hash": fhash,
    }
    pa = await _create_pending(user, update.effective_chat.id, PendingActionType.confirm_import, payload)  # type: ignore[union-attr]
    total_cost = sum((r.quantity * r.cost_per_share + r.fee for r in rows), Decimal("0"))
    lines = [f"📥 **导入预览（{len(rows)} 行）**\n", "代码 | 数量 | 成本价 | 小计", ""]
    for r in rows[:15]:
        sub = r.quantity * r.cost_per_share
        lines.append(f"`{r.symbol}` {r.quantity} @ {r.cost_per_share} → {sub:,.2f}")
    if len(rows) > 15:
        lines.append(f"…（其余 {len(rows) - 15} 行省略）")
    lines += ["", f"**合计投入成本：¥{total_cost:,.2f}**", "", f"⏳ 30 分钟内确认有效（编号 {pa.token}）"]
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=_confirm_keyboard(pa.token)
    )
    context.user_data["awaiting_import"] = False  # type: ignore[index]


# ---------------------------------------------------------------- M1：交易


async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    await update.message.reply_text(
        "✏️ **交易入账**（登记已完成交易，不是下单）\n\n"
        "直接发送一条描述，例如：\n"
        "• `买入 600036.SH 100 32.50 费用5`\n"
        "• `卖出 000001.SZ 200 11.80`\n"
        "• `入金 100000`\n"
        "• `出金 50000`\n"
        "• `分红 600036.SH 840`\n\n"
        "默认日期为今天，可加日期：`买入 600036.SH 100 32.5 2026-11-25`\n"
        "我解析后会展示核对卡片，你确认后才写入账本。",
        parse_mode="Markdown",
    )


def _trade_preview_card(pv: TradePreview) -> str:
    lines = [
        "📝 **请核对交易记录**（仅登记，不涉及下单）\n",
        f"方向：**{DIR_LABELS[pv.direction]}**",
    ]
    if pv.symbol:
        lines.append(f"代码：`{pv.symbol}`")
    if pv.quantity is not None:
        lines.append(f"数量：{pv.quantity} 股")
    if pv.price is not None:
        lines.append(f"成交价：{pv.price} 元")
    if pv.amount is not None:
        lines.append(f"金额：{pv.amount:,.2f} 元")
    if pv.fee:
        lines.append(f"费用：{pv.fee} 元")
    if pv.direction in (TradeDirection.buy, TradeDirection.sell):
        gross = (pv.quantity or Decimal("0")) * (pv.price or Decimal("0"))
        lines.append(f"成交金额：{gross:,.2f} 元")
    lines += [f"现金变动：**{pv.cash_delta:,.2f} 元**", f"日期：{pv.trade_date.isoformat()}"]
    return "\n".join(lines)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None and update.effective_chat is not None
    txt = (update.message.text or "").strip()
    loaded = await _load_user(update)
    if loaded is None:
        return
    user, _portfolio = loaded
    parsed = parse_trade_text(txt)
    if isinstance(parsed, str):
        if context.user_data.get("awaiting_import"):  # type: ignore[attr-defined]
            await update.message.reply_text("仍处于导入模式：请上传 CSV 文件，或发送 /cancel 取消。")
        elif "未识别方向" in parsed:
            await update.message.reply_text(
                "🤖 我暂未理解这条消息。\n\n"
                "可用操作：/import 导入持仓、/trade 登记交易、/portfolio 查看持仓、/help 帮助。"
            )
        else:
            await update.message.reply_text(f"⚠️ {parsed}")
        return
    pv = parsed
    payload = {
        "direction": pv.direction.value,
        "symbol": pv.symbol,
        "quantity": str(pv.quantity) if pv.quantity is not None else None,
        "price": str(pv.price) if pv.price is not None else None,
        "gross_amount": (
            str(pv.quantity * pv.price)
            if pv.quantity is not None and pv.price is not None
            else (str(pv.amount) if pv.amount is not None else None)
        ),
        "fee": str(pv.fee),
        "cash_delta": str(pv.cash_delta),
        "trade_date": pv.trade_date.isoformat(),
        "note": DIR_LABELS[pv.direction],
    }
    pa = await _create_pending(user, update.effective_chat.id, PendingActionType.confirm_trade, payload)
    await update.message.reply_text(
        _trade_preview_card(pv) + f"\n\n⏳ 30 分钟内确认有效（编号 {pa.token}）",
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard(pa.token),
    )


async def _user_id_of(update: Update) -> int:
    async with Session() as session:
        user = (
            await session.execute(
                select(User).where(User.telegram_user_id == update.effective_user.id)  # type: ignore[union-attr]
            )
        ).scalar_one()
        return user.id


async def _edit_done(query, body: str) -> None:
    try:
        await query.edit_message_text(body, parse_mode="Markdown")
    except Exception:  # noqa: BLE001
        await query.message.reply_text(body, parse_mode="Markdown")  # type: ignore[union-attr]


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """确认/取消按钮：鉴权、过期校验、幂等状态机。"""
    query = update.callback_query
    if query is None or not _authorized(update):
        if query:
            await query.answer("未授权", show_alert=True)
        return
    assert query.message is not None and update.effective_user is not None
    _, action, token = query.data.split(":")  # type: ignore[union-attr]
    async with Session() as session:
        pa = (
            await session.execute(
                select(PendingAction).where(PendingAction.token == token).with_for_update()
            )
        ).scalar_one_or_none()
        real_uid = await _user_id_of(update)
        if pa is None or pa.user_id != real_uid:
            await query.answer("记录不存在", show_alert=True)
            return
        if pa.status != PendingStatus.pending:
            await query.answer("该操作已处理，请勿重复点击", show_alert=True)
            return
        now = _utcnow()
        if action == "cancel":
            pa.status = PendingStatus.cancelled
            await session.commit()
            await query.answer("已取消")
            await _edit_done(query, f"❌ 已取消操作（编号 {pa.token}）")
            return
        if pa.expires_at < now:
            pa.status = PendingStatus.expired
            await session.commit()
            await query.answer("已过期", show_alert=True)
            await _edit_done(query, f"⌛ 操作已过期（编号 {pa.token}），请重新发起。")
            return
        loaded = await _load_user(update)
        if loaded is None:
            await query.answer("未初始化", show_alert=True)
            return
        _user, portfolio = loaded
        if pa.action_type == PendingActionType.confirm_import:
            from app.services.portfolio_service import ImportRow

            rows = [
                ImportRow(
                    row_no=r["row_no"],
                    symbol=r["symbol"],
                    quantity=Decimal(r["quantity"]),
                    cost_per_share=Decimal(r["cost_per_share"]),
                    fee=Decimal(r["fee"]),
                    currency=r["currency"],
                    as_of=date.fromisoformat(r["as_of"]),
                )
                for r in pa.payload["rows"]
            ]
            n = await confirm_import(session, pa, portfolio, rows, pa.payload["file_hash"])
            await session.commit()
            await query.answer("入账成功")
            await _edit_done(
                query,
                f"✅ **导入完成**：已写入 {n} 条初始建仓记录，持仓已更新。\n发送 /portfolio 查看。",
            )
        else:
            entry = await confirm_trade(session, pa, portfolio)
            await session.commit()
            await query.answer("入账成功")
            qty_txt = "" if entry.quantity is None else f" {entry.quantity}股"
            await _edit_done(
                query,
                f"✅ **已入账**（流水号 {entry.id}）\n{DIR_LABELS[entry.direction]} "
                f"{entry.symbol or ''}{qty_txt}"
                f"，现金变动 {entry.cash_delta:,.2f} 元\n发送 /portfolio 查看持仓。",
            )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    context.user_data["awaiting_import"] = False  # type: ignore[index]
    uid = await _user_id_of(update)
    async with Session() as session:
        pa = (
            await session.execute(
                select(PendingAction)
                .where(PendingAction.user_id == uid, PendingAction.status == PendingStatus.pending)
                .order_by(PendingAction.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if pa is None:
            await update.message.reply_text("没有待确认的操作。")
            return
        pa.status = PendingStatus.cancelled
        await session.commit()
        token = pa.token
    await update.message.reply_text(f"❌ 已取消操作（编号 {token}）。")


# ---------------------------------------------------------------- M1：持仓


async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    loaded = await _load_user(update)
    if loaded is None:
        return
    _user, portfolio = loaded
    async with Session() as session:
        positions, cash = await portfolio_snapshot(session, portfolio.id)
    if not positions and cash == Decimal("0"):
        await update.message.reply_text(
            "📊 **我的持仓**\n\n暂无持仓记录。\n发送 /import 导入现有持仓，或 /trade 登记交易。",
            parse_mode="Markdown",
        )
        return
    lines = ["📊 **我的持仓**\n", "代码 | 数量 | 均价成本 | 成本金额", ""]
    total_cost = Decimal("0")
    for p in positions:
        total_cost += p.total_cost
        lines.append(f"`{p.symbol}`  {p.quantity}  @ {_fmt(p.avg_cost)}  → {_fmt(p.total_cost)}")
    lines += [
        "",
        f"持仓成本合计：¥{_fmt(total_cost)}",
        f"现金余额：¥{_fmt(cash)}",
        f"资产合计（成本口径）：¥{_fmt(total_cost + cash)}",
        "",
        "ℹ️ 行情数据模块上线后显示最新市值与盈亏。",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
