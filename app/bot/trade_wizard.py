"""交易入账对话向导：方向 → 搜索代码 → 数量 → 价格 → 费用 → 确认卡。"""
import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.services.ledger_service import TradePreview
from app.services.ledger_service import parse_trade_text as _legacy_parse  # noqa: F401
from app.services.search_service import search_securities
from app.storage.models import TradeDirection
from app.storage.db import Session

log = logging.getLogger(__name__)

DIRECTION, SYMBOL, QUANTITY, PRICE, FEE, CASH_SYMBOL, CASH_AMOUNT = range(7)

def _dir_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("买入", callback_data="tw:dir:buy"),
                InlineKeyboardButton("卖出", callback_data="tw:dir:sell"),
            ],
            [
                InlineKeyboardButton("入金", callback_data="tw:dir:deposit"),
                InlineKeyboardButton("出金", callback_data="tw:dir:withdraw"),
                InlineKeyboardButton("分红", callback_data="tw:dir:dividend"),
            ],
        ]
    )


_DIR_LABELS = {
    TradeDirection.buy: "买入",
    TradeDirection.sell: "卖出",
    TradeDirection.deposit: "入金",
    TradeDirection.withdraw: "出金",
    TradeDirection.dividend: "分红",
}
_STOCK_DIRS = {"buy", "sell", "dividend"}


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    context.user_data.pop("tw", None)  # type: ignore[attr-defined]
    await update.message.reply_text(
        "✏️ **交易入账向导**（登记已完成交易，不是下单）\n\n请选择方向：",
        parse_mode="Markdown",
        reply_markup=_dir_keyboard(),
    )
    return DIRECTION


async def text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await entry(update, context)


async def choose_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    _, _, key = query.data.split(":")  # type: ignore[union-attr]
    tw = context.user_data.setdefault("tw", {})  # type: ignore[attr-defined]
    tw["dir"] = key
    direction = TradeDirection(key)
    if key in ("buy", "sell"):
        await query.edit_message_text(
            f"方向：**{_DIR_LABELS[direction]}**（1/3）\n\n"
            "请输入股票：6 位代码、名称或拼音（如 `600036`、`招行`、`zsyh`）",
            parse_mode="Markdown",
        )
        return SYMBOL
    if key == "dividend":
        await query.edit_message_text(
            "方向：**分红**\n\n"
            "分红到账是否关联股票？输入代码/名称搜索，或点“不关联”：",
            parse_mode="Markdown",
            reply_markup=_kb([[InlineKeyboardButton("不关联股票", callback_data="tw:sym:skip")]]),
        )
        return CASH_SYMBOL
    # 入金 / 出金
    await query.edit_message_text(
        f"方向：**{_DIR_LABELS[direction]}**\n\n请输入金额（元），如 `100000`：",
        parse_mode="Markdown",
    )
    return CASH_AMOUNT


def _symbol_kb(matches: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{m['name']}（{m['symbol']}）", callback_data=f"tw:sym:{m['symbol']}")]
        for m in matches[:8]
    ]
    rows.append([InlineKeyboardButton("« 取消", callback_data="tw:cancel")])
    return _kb(rows)


async def symbol_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    q = (update.message.text or "").strip()
    tw = context.user_data["tw"]  # type: ignore[attr-defined]
    async with Session() as session:
        matches = await search_securities(session, q)
        await session.commit()
    if not matches:
        await update.message.reply_text("未找到匹配的股票，换个关键词试试（如 `600036` / `招行` / `zsyh`）：")
        return SYMBOL
    await update.message.reply_text("请点击选择：", reply_markup=_symbol_kb(matches))
    return SYMBOL


async def symbol_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    symbol = query.data.split(":", 2)[2]  # type: ignore[union-attr]
    tw = context.user_data["tw"]  # type: ignore[attr-defined]
    if symbol == "skip":
        tw["symbol"] = None
        tw["name"] = None
        await query.edit_message_text("不关联股票。请输入金额（元），如 `840`：")
        return CASH_AMOUNT
    tw["symbol"] = symbol
    tw["name"] = None  # 从按钮文本回查名称
    try:
        parts = query.message.reply_markup.inline_keyboard if query.message and query.message.reply_markup else None
        if parts:
            for row in parts:
                for btn in row:
                    if btn.callback_data == query.data and "（" in btn.text:
                        tw["name"] = btn.text.split("（")[0]
    except Exception:  # noqa: BLE001
        pass
    await query.edit_message_text(
        f"股票：**{symbol}**（2/3）\n\n请输入数量（股），如 `100`：",
        parse_mode="Markdown",
    )
    return QUANTITY


def _parse_decimal(text: str) -> Decimal | None:
    try:
        v = Decimal(text.strip().replace("股", "").replace("元", "").strip())
        return v if v > 0 else None
    except (InvalidOperation, ValueError):
        return None


async def quantity_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    v = _parse_decimal(update.message.text or "")
    if v is None:
        await update.message.reply_text("⚠️ 数量必须是大于 0 的数字，请重新输入：")
        return QUANTITY
    context.user_data["tw"]["quantity"] = v  # type: ignore[attr-defined]
    await update.message.reply_text(
        "请输入成交价（元），如 `32.50`：",
    )
    return PRICE


async def price_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    v = _parse_decimal(update.message.text or "")
    if v is None:
        await update.message.reply_text("⚠️ 价格必须是大于 0 的数字，请重新输入：")
        return PRICE
    context.user_data["tw"]["price"] = v  # type: ignore[attr-defined]
    await update.message.reply_text(
        "请输入费用（元，可小数）；或点跳过：",
        reply_markup=_kb([[InlineKeyboardButton("无费用，跳过", callback_data="tw:fee:skip")]]),
    )
    return FEE


async def fee_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    text = (update.message.text or "").strip()
    if text.lower() in ("skip", "跳过", "无", "0"):
        fee = Decimal("0")
    else:
        try:
            fee = Decimal(text)
            if fee < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            await update.message.reply_text("⚠️ 费用需为不小于 0 的数字：")
            return FEE
    tw = context.user_data["tw"]  # type: ignore[attr-defined]
    return await _finalize(update, context, tw, fee)


async def fee_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    tw = context.user_data["tw"]  # type: ignore[attr-defined]
    return await _finalize(update, context, tw, Decimal("0"), via_query=query)


async def cash_amount_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    v = _parse_decimal(update.message.text or "")
    if v is None:
        await update.message.reply_text("⚠️ 金额必须是大于 0 的数字，请重新输入：")
        return CASH_AMOUNT
    tw = context.user_data["tw"]  # type: ignore[attr-defined]
    return await _finalize(update, context, tw, Decimal("0"), amount=v)


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, tw: dict, fee: Decimal,
                    amount: Decimal | None = None, via_query=None) -> int:
    from app.bot.handlers import _create_pending, _confirm_keyboard, _load_user
    from app.bot.handlers import _trade_preview_card

    assert update.effective_chat is not None and update.effective_user is not None
    direction = TradeDirection(tw["dir"])
    symbol = tw.get("symbol")
    quantity = tw.get("quantity")
    price = tw.get("price")
    from datetime import date as _date

    pv = TradePreview(
        direction=direction, symbol=symbol,
        quantity=quantity, price=price, amount=amount,
        fee=fee, trade_date=_date.today(),
        cash_delta=_cash_delta(direction, quantity, price, amount, fee),
    )
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
        "note": _DIR_LABELS[pv.direction],
    }
    loaded = await _load_user(update)
    if loaded is None:
        return ConversationHandler.END
    user, _pf = loaded
    from app.storage.models import PendingActionType

    pa = await _create_pending(user, update.effective_chat.id, PendingActionType.confirm_trade, payload)
    card = _trade_preview_card(pv, name=tw.get("name")) + f"\n\n⏳ 30 分钟内确认有效（编号 {pa.token}）"
    kb = _confirm_keyboard(pa.token)
    if via_query is not None:
        await via_query.edit_message_text(card, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(card, parse_mode="Markdown", reply_markup=kb)  # type: ignore[union-attr]
    context.user_data.pop("tw", None)  # type: ignore[attr-defined]
    return ConversationHandler.END


def _cash_delta(direction, quantity, price, amount, fee) -> Decimal:
    from decimal import Decimal as _D

    if direction == TradeDirection.buy:
        return -((quantity or _D(0)) * (price or _D(0)) + fee)
    if direction == TradeDirection.sell:
        return (quantity or _D(0)) * (price or _D(0)) - fee
    if direction == TradeDirection.withdraw:
        return -(amount or _D(0))
    return (amount or _D(0))  # deposit / dividend


async def wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("tw", None)  # type: ignore[attr-defined]
    if update.message:
        await update.message.reply_text("❌ 已退出交易向导。", reply_markup=None)
    if update.callback_query:
        await update.callback_query.answer("已取消")
        try:
            await update.callback_query.edit_message_text("❌ 已退出交易向导。")
        except Exception:  # noqa: BLE001
            pass
    return ConversationHandler.END


def build_trade_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("trade", entry),
            MessageHandler(filters.Regex(r"^(✏️ ?交易入账|交易入账|交易)$"), text_entry),
        ],
        states={
            DIRECTION: [CallbackQueryHandler(choose_direction, pattern=r"^tw:dir:")],
            SYMBOL: [
                CallbackQueryHandler(symbol_choose, pattern=r"^tw:sym:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, symbol_search),
            ],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_step)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, price_step)],
            FEE: [
                CallbackQueryHandler(fee_skip, pattern=r"^tw:fee:skip"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fee_step),
            ],
            CASH_SYMBOL: [
                CallbackQueryHandler(symbol_choose, pattern=r"^tw:sym:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, symbol_search),
            ],
            CASH_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_amount_step)],
        },
        fallbacks=[
            CommandHandler("cancel", wizard_cancel),
            CallbackQueryHandler(wizard_cancel, pattern=r"^tw:cancel"),
            MessageHandler(filters.Regex(r"^(取消|退出)$"), wizard_cancel),
        ],
        per_message=False,
        name="trade_wizard",
    )
