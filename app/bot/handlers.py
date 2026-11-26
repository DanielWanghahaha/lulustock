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
from app.services.quotes_service import latest_closes, latest_trading_date, persist_snapshot, update_quotes
from app.services.report_service import build_daily_brief
from app.services.watchlist_service import (
    add_watch,
    list_watchlist,
    parse_alerts,
    remove_watch,
)
from app.services.user_service import ensure_policy_v1, ensure_user, get_default_portfolio
from app.storage.db import Session
from app.storage.models import DailyQuote, OutboxMessage
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
        "/watchlist — 观察池与价格触发\n"
        "/research 代码 — 个股研究\n"
        "/valuation 代码 — 三情景估值\n"
        "/today — 盘后简报\n"
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
            last_trade_day = await latest_trading_date(session)
            outbox_pending = (
                await session.execute(text("SELECT COUNT(*) FROM outbox WHERE status = 'pending'"))
            ).scalar()
            outbox_failed = (
                await session.execute(text("SELECT COUNT(*) FROM outbox WHERE status = 'failed'"))
            ).scalar()
        lines.append(f"✅ 数据库：MySQL {version}（{db_ms:.0f} ms）")
        lines.append(f"✅ 数据表：{table_count} 张")
        lines.append(f"⏳ 待确认操作：{pending} 个")
        lines.append(f"📡 行情截止：{last_trade_day or '无数据'}")
        lines.append(f"📤 通知队列：待发 {outbox_pending} / 失败 {outbox_failed}")
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
    if await _handle_watchlist_intent(update, txt, user):
        return
    if await _handle_research_intent(update, txt):
        return
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
        closes = await latest_closes(session, [p.symbol for p in positions])
    if not positions and cash == Decimal("0"):
        await update.message.reply_text(
            "📊 **我的持仓**\n\n暂无持仓记录。\n发送 /import 导入现有持仓，或 /trade 登记交易。",
            parse_mode="Markdown",
        )
        return
    lines = ["📊 **我的持仓**", ""]
    total_cost = Decimal("0")
    total_value = Decimal("0")
    have_all_quotes = all(p.symbol in closes for p in positions)
    for p in positions:
        total_cost += p.total_cost
        c = closes.get(p.symbol)
        if c is not None:
            d, close = c
            value = p.quantity * close
            total_value += value
            pnl = (close - p.avg_cost) / p.avg_cost if p.avg_cost else Decimal("0")
            lines.append(
                f"`{p.symbol}`  {p.quantity}股  成本{_fmt(p.avg_cost)} → 现价{_fmt(close)}（{d}）"
                f"  市值 {_fmt(value)}  盈亏 {pnl * 100:+.2f}%"
            )
        else:
            lines.append(f"`{p.symbol}`  {p.quantity}股  成本{_fmt(p.avg_cost)} → 行情缺失（按成本 {_fmt(p.total_cost)} 计）")
    lines += ["", f"持仓成本合计：¥{_fmt(total_cost)}"]
    if have_all_quotes and positions:
        lines += [
            f"持仓市值合计：¥{_fmt(total_value)}",
            f"现金余额：¥{_fmt(cash)}",
            f"组合净值：¥{_fmt(total_value + cash)}",
        ]
    else:
        lines += [
            f"现金余额：¥{_fmt(cash)}",
            f"资产合计（成本口径）：¥{_fmt(total_cost + cash)}",
            "⚠️ 部分持仓缺行情，发送 /update_quotes 更新后显示市值。",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------- M2：观察池


async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    loaded = await _load_user(update)
    if loaded is None:
        return
    user, _pf = loaded
    async with Session() as session:
        items = await list_watchlist(session, user.id)
    if not items:
        await update.message.reply_text(
            "⭐ **投资观察池**（空）\n\n"
            "添加方式：发送 `添加 600036.SH 上限45 下限38`\n"
            "（上限/下限为可选价格触发线）\n"
            "移除方式：`移除 600036.SH`",
            parse_mode="Markdown",
        )
        return
    lines = ["⭐ **投资观察池**\n"]
    for it in items:
        alerts = []
        if it.price_alert_high is not None:
            alerts.append(f"上限 {it.price_alert_high}")
        if it.price_alert_low is not None:
            alerts.append(f"下限 {it.price_alert_low}")
        lines.append(f"`{it.symbol}` {' / '.join(alerts) if alerts else '无价格触发'}")
    lines += ["", "添加：`添加 600036.SH 上限45 下限38`；移除：`移除 600036.SH`"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _handle_watchlist_intent(update: Update, txt: str, user) -> bool:
    """识别 添加/移除 观察池意图；处理成功返回 True。"""
    add = None
    for kw in ("添加", "关注", "加入观察池", "加入"):
        if txt.startswith(kw):
            add = True
            break
    if add is None:
        for kw in ("移除", "取消关注", "删除"):
            if txt.startswith(kw):
                add = False
                break
    if add is None:
        return False
    rest = txt.split(maxsplit=1)[1] if len(txt.split(maxsplit=1)) > 1 else ""
    import re as _re

    sm = _re.search(r"\d{6}(?:\.(?:SH|SZ|BJ))?", rest, _re.IGNORECASE)
    if sm is None:
        if update.message:
            await update.message.reply_text("请提供股票代码，例如：添加 600036.SH 上限45 下限38")
        return True
    symbol = sm.group(0)
    high, low, err = parse_alerts(rest)
    if err:
        if update.message:
            await update.message.reply_text(f"⚠️ {err}")
        return True
    async with Session() as session:
        if add:
            item, error = await add_watch(session, user.id, symbol, note=None, high=high, low=low)
            await session.commit()
            if error:
                if update.message:
                    await update.message.reply_text(f"⚠️ {error}")
            elif update.message:
                alerts = []
                if high is not None:
                    alerts.append(f"收盘 ≥ {high} 触发")
                if low is not None:
                    alerts.append(f"收盘 ≤ {low} 触发")
                alert_txt = "；".join(alerts) if alerts else "未设置价格触发"
                await update.message.reply_text(
                    f"⭐ 已加入观察池：`{item.symbol}`\n触发规则：{alert_txt}\n"
                    "（触发为研究提醒，不等于买入建议）",
                    parse_mode="Markdown",
                )
        else:
            ok, error = await remove_watch(session, user.id, symbol)
            await session.commit()
            if update.message:
                await update.message.reply_text(f"🗑 已从观察池移除：`{symbol}`" if ok else f"⚠️ {error}", parse_mode="Markdown")
    return True


# ---------------------------------------------------------------- M2：简报与行情


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    loaded = await _load_user(update)
    if loaded is None:
        return
    user, portfolio = loaded
    msg = await update.message.reply_text("⏳ 正在更新行情并生成简报…")
    try:
        async with Session() as session:
            result = await update_quotes(session)
            await persist_snapshot(session, portfolio)
            await session.commit()
        async with Session() as session:
            brief = await build_daily_brief(session, user.id, portfolio)
    except Exception as exc:  # noqa: BLE001
        log.exception("today brief failed")
        await msg.edit_text(f"❌ 简报生成失败：{type(exc).__name__}: {exc}")
        return
    await msg.edit_text(brief, parse_mode="Markdown")


async def update_quotes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    loaded = await _load_user(update)
    if loaded is None:
        return
    _user, portfolio = loaded
    msg = await update.message.reply_text("⏳ 正在更新行情…")
    try:
        async with Session() as session:
            result = await update_quotes(session)
            snap = await persist_snapshot(session, portfolio)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.exception("update quotes failed")
        await msg.edit_text(f"❌ 行情更新失败：{type(exc).__name__}: {exc}")
        return
    latest_txt = "\n".join(
        f"• `{s}` {d} 收盘 {c}" for s, (d, c) in result["latest"].items()
    ) or "（无代码）"
    unsupported_note = f"\n⚠️ {result['unsupported']} 个北交所代码暂不支持" if result["unsupported"] else ""
    await msg.edit_text(
        f"📡 **行情更新完成**\n"
        f"成功 {result['ok']} / 失败 {result['failed']}{unsupported_note}\n\n{latest_txt}\n\n"
        f"💾 今日快照：现金 {snap.cash:,.2f} + 市值 {snap.market_value:,.2f} = 净值 {snap.nav:,.2f}"
        + ("" if snap.complete else "\n⚠️ 部分持仓缺行情，快照标记为不完整"),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------- M3：研究

import re as _re  # noqa: E402

from telegram import InlineKeyboardButton as _IKB  # noqa: E402
from telegram import Update as _Upd  # noqa: E402
from app.services.research_service import CONCLUSION_LABELS, run_research  # noqa: E402
from app.services.portfolio_service import normalize_symbol as _norm_sym  # noqa: E402

_RESEARCH_INTENT_RE = _re.compile(r"^(?:研究|分析|估值)\s*[:：]?\s*(.+)$", _re.IGNORECASE)


def _research_card(payload: dict, symbol: str, version: int, mode: str) -> str:
    title = "研究卡" if mode == "research" else "估值卡"
    lines = [
        f"🔍 **{payload.get('company_name') or symbol}** ({symbol}) {title} v{version}",
        f"结论：**{CONCLUSION_LABELS.get(payload['conclusion'], payload['conclusion'])}**",
        "",
        f"📄 {payload.get('summary', '')}",
        "",
        "**投资逻辑：**",
    ]
    for t in payload.get("thesis", [])[:4]:
        conf = t.get("confidence", "medium") if isinstance(t, dict) else "medium"
        point = t.get("point") if isinstance(t, dict) else str(t)
        lines.append(f"• {point}（置信：{conf}）")
    lines.append("**反方观点：**")
    for c in payload.get("counterarguments", [])[:4]:
        lines.append(f"• {c}")
    val = payload.get("valuation") or {}
    sc = val.get("scenarios") or {}
    if sc:
        lines.append(
            "**三情景估值（假设性，缺财务数据）：**\n"
            f"悲观 {sc.get('pessimistic', {}).get('price')} / 中性 {sc.get('neutral', {}).get('price')} / 乐观 {sc.get('optimistic', {}).get('price')}"
        )
    missing = payload.get("missing_data") or []
    if missing:
        lines.append(f"\n⚠️ 缺失数据：{'、'.join(missing[:5])}")
    return "\n".join(lines[:40])


async def _run_and_send_research(update: Update, symbol_raw: str, mode: str) -> None:
    assert update.message is not None and update.effective_chat is not None
    symbol = _norm_sym(symbol_raw)
    if symbol is None:
        await update.message.reply_text("⚠️ 请提供有效的 6 位股票代码，如：研究 600036.SH")
        return
    loaded = await _load_user(update)
    if loaded is None:
        return
    user, _pf = loaded
    wait_txt = (
        f"⏳ 正在研究 `{symbol}`（调用 LLM，约 30-60 秒）…"
        if mode == "research"
        else f"⏳ 正在为 `{symbol}` 生成三情景估值（约 30-60 秒）…"
    )
    msg = await update.message.reply_text(wait_txt, parse_mode="Markdown")
    try:
        async with Session() as session:
            result = await run_research(session, user.id, symbol, mode=mode)
            await session.commit()
    except LLMError as exc:
        await msg.edit_text(f"❌ 研究失败：{exc}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("research failed")
        await msg.edit_text(f"❌ 研究失败：{type(exc).__name__}: {exc}")
        return
    if "error" in result:
        await msg.edit_text(f"⚠️ {result['error']}")
        return
    payload = result["payload"]
    card = _research_card(payload, symbol, result["version"], mode)
    try:
        await msg.edit_text(card, parse_mode="Markdown", reply_markup=_research_keyboard(symbol))
    except Exception:  # noqa: BLE001
        await msg.edit_text(card, reply_markup=_research_keyboard(symbol))
    # 完整报告以文件发送
    import tempfile

    from telegram import InputFile

    md = _full_report_md(payload, symbol, result["version"], result["price"])
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(md)
        path = f.name
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(f, filename=f"research_{symbol}_v{result['version']}.md")
    finally:
        import os

        os.unlink(path)


def _research_keyboard(symbol: str):
    return InlineKeyboardMarkup(
        [[_IKB("⭐ 加入观察池", callback_data=f"wladd:{symbol}")]]
    )


def _full_report_md(payload: dict, symbol: str, version: int, price: dict) -> str:
    lines = [
        f"# {payload.get('company_name') or symbol} 研究报告 v{version}",
        "",
        f"> 标的：{symbol} | 生成时间：{date.today().isoformat()} | 结论：{CONCLUSION_LABELS.get(payload['conclusion'], payload['conclusion'])}",
        "",
        f"## 行情数据（确定性）",
        f"```json\n{price}\n```",
        "",
        f"## 摘要\n{payload.get('summary', '')}",
        "",
        "## 投资逻辑",
    ]
    for t in payload.get("thesis", []):
        if isinstance(t, dict):
            lines.append(f"- {t.get('point')}（置信：{t.get('confidence')}）")
        else:
            lines.append(f"- {t}")
    lines += ["", "## 反方观点"]
    for c in payload.get("counterarguments", []):
        lines.append(f"- {c}")
    lines += ["", "## 主要风险"]
    for r in payload.get("risks", []):
        lines.append(f"- {r}")
    lines += ["", "## 逻辑失效条件"]
    for r in payload.get("invalidation_conditions", []):
        lines.append(f"- {r}")
    val = payload.get("valuation") or {}
    sc = val.get("scenarios") or {}
    lines += [
        "",
        "## 三情景估值（假设性，待财务数据验证）",
        f"方法：{val.get('method', '')}",
        "",
    ]
    for k, label in (("pessimistic", "悲观"), ("neutral", "中性"), ("optimistic", "乐观")):
        s = sc.get(k) or {}
        lines.append(f"- **{label}**：{s.get('price')} — {s.get('rationale', '')}")
    lines += ["", "## 关键假设"]
    for a in val.get("assumptions", []):
        lines.append(f"- {a}")
    if val.get("warning"):
        lines += ["", f"⚠️ {val['warning']}"]
    lines += ["", "## 缺失数据"]
    for m in payload.get("missing_data", []):
        lines.append(f"- {m}")
    lines += [
        "",
        "---",
        "声明：本报告由 AI 生成，仅用于个人研究参考，不构成投资建议；定性内容未经核验，财务数据缺失，估值结论为假设性。",
    ]
    return "\n".join(lines)


async def research_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    arg = (context.args or [""])[0] if context.args else ""
    if not arg:
        await update.message.reply_text("用法：`/research 600036.SH`", parse_mode="Markdown")
        return
    await _run_and_send_research(update, arg, mode="research")


async def valuation_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    assert update.message is not None
    arg = (context.args or [""])[0] if context.args else ""
    if not arg:
        await update.message.reply_text("用法：`/valuation 600036.SH`", parse_mode="Markdown")
        return
    await _run_and_send_research(update, arg, mode="valuation")


async def _handle_research_intent(update: Update, txt: str) -> bool:
    m = _RESEARCH_INTENT_RE.match(txt)
    if m is None:
        return False
    target = m.group(1).strip()
    mode = "valuation" if txt.startswith("估值") else "research"
    if not _re.match(r"^\d{6}", target):
        return False  # 名称解析留待后续；无代码则走其他分支
    await _run_and_send_research(update, target, mode=mode)
    return True


async def on_wladd_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not _authorized(update):
        if query:
            await query.answer("未授权", show_alert=True)
        return
    symbol = query.data.split(":", 1)[1]  # type: ignore[union-attr]
    loaded = await _load_user(update)
    if loaded is None:
        return
    user, _pf = loaded
    async with Session() as session:
        item, error = await add_watch(session, user.id, symbol)
        await session.commit()
    if error:
        await query.answer(error, show_alert=True)
        return
    await query.answer("已加入观察池")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
