"""报告：盘后简报（/today 与定时任务共用）。"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.quotes_service import latest_trading_date, persist_snapshot
from app.services.risk_engine import Finding, evaluate_portfolio
from app.storage.models import Portfolio, Position, DailyQuote

LEVEL_ICONS = {"red": "🔴", "yellow": "🟡", "info": "🔵"}


async def build_daily_brief(session: AsyncSession, user_id: int, portfolio: Portfolio) -> str:
    positions = (
        (await session.execute(select(Position).where(Position.portfolio_id == portfolio.id).order_by(Position.symbol)))
        .scalars()
        .all()
    )
    findings, summary = await evaluate_portfolio(session, user_id, portfolio.id, None)
    trade_day = await latest_trading_date(session)

    lines = ["📋 **盘后简报**\n"]
    if trade_day is None:
        lines.append("⚠️ 暂无行情数据。发送 /update_quotes 更新行情后查看完整简报。")
        if not positions:
            lines.append("\n组合尚未初始化：发送 /import 导入持仓。")
        return "\n".join(lines)

    lines.append(f"数据截止：{trade_day} 收盘")
    nav = summary["nav"]
    if nav and nav > 0:
        lines.append(
            f"组合净值：¥{nav:,.2f}（现金 {summary['cash']:,.0f} + 持仓市值 {summary['market_value']:,.0f}）"
        )
        if summary.get("cash_weight") is not None:
            lines.append(f"现金占比：{summary['cash_weight'] * 100:.1f}%")
        if summary.get("current_drawdown") is not None:
            lines.append(f"当前回撤：{summary['current_drawdown'] * 100:.1f}%")
    else:
        lines.append("组合表现：暂无可计算数据")

    if findings:
        lines.append("\n**风险与关注：**")
        for f in findings:
            lines.append(f"{LEVEL_ICONS[f.level]} [{f.code}] {f.message}")
    else:
        lines.append("\n结论：暂无需要立即决策的事项")

    if summary["stale_symbols"]:
        lines.append("\nℹ️ 行情缺失的持仓按成本口径计，不构成精确仓位。")
    return "\n".join(lines)
