"""总账聚合：持仓成本与现金的确定性计算。"""
from dataclasses import dataclass
from decimal import Decimal

from app.storage.models import TradeDirection

ZERO = Decimal("0")


@dataclass
class EntryRow:
    """一条总账分录的最小计算视图。"""

    trade_date: object
    direction: TradeDirection
    symbol: str | None
    quantity: Decimal | None
    price: Decimal | None
    fee: Decimal = ZERO
    cash_delta: Decimal = ZERO


@dataclass
class PositionAgg:
    """聚合后的持仓（移动加权成本）。"""

    symbol: str
    quantity: Decimal = ZERO
    total_cost: Decimal = ZERO
    avg_cost: Decimal = ZERO


def aggregate_positions(entries: list[EntryRow]) -> dict[str, PositionAgg]:
    """按 (trade_date, id) 有序分录聚合持仓；卖出按剩余成本分摊。"""
    aggs: dict[str, PositionAgg] = {}
    for e in entries:
        if not e.symbol:
            continue
        qty = e.quantity or ZERO
        price = e.price or ZERO
        agg = None
        if e.direction in (TradeDirection.seed, TradeDirection.buy):
            agg = aggs.setdefault(e.symbol, PositionAgg(symbol=e.symbol))
            agg.quantity += qty
            agg.total_cost += qty * price + (e.fee if e.direction == TradeDirection.buy else ZERO)
        elif e.direction == TradeDirection.sell:
            agg = aggs.get(e.symbol)
            if agg is None or agg.quantity <= 0:
                continue  # 卖出超过持仓：忽略并在对账时报告
            avg = agg.total_cost / agg.quantity
            sold_qty = min(qty, agg.quantity)
            agg.quantity -= sold_qty
            agg.total_cost -= avg * sold_qty
        if agg is not None and agg.quantity > 0:
            agg.avg_cost = agg.total_cost / agg.quantity
    return aggs


def compute_cash(entries: list[EntryRow]) -> Decimal:
    """现金余额 = 所有分录 cash_delta 之和。"""
    return sum((e.cash_delta for e in entries), ZERO)
