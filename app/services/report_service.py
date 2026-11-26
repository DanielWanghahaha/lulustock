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


# ---------------------------------------------------------------- M4：周报 / 月报


def _period_range(kind: str, ref=None) -> tuple:
    """返回 (start_date, end_date, period_label)。"""
    from datetime import date as _date, timedelta as _td

    ref = ref or _date.today()
    if kind == "weekly":
        start = ref - _td(days=ref.weekday() + 7)  # 上周一
        end = start + _td(days=6)  # 上周日
        iso = start.isocalendar()
        label = f"{iso[0]}-W{iso[1]:02d}"
        return start, end, label
    # monthly：上个自然月
    first_this = _date(ref.year, ref.month, 1)
    end = first_this - _td(days=1)
    start = _date(end.year, end.month, 1)
    label = f"{start.year}-{start.month:02d}"
    return start, end, label


async def build_period_report(session: AsyncSession, user_id: int, portfolio_id: int, kind: str) -> str:
    from datetime import date as _date

    from sqlalchemy import select as _select

    from app.storage.models import (
        LedgerEntry,
        PortfolioSnapshot,
        Position,
        ResearchVersion,
        RiskEvent,
    )

    start, end, label = _period_range(kind)
    lines = [f"📊 **{'投资周报' if kind == 'weekly' else '投资月报'}**（{label}）\n"]

    snaps = (
        (
            await session.execute(
                _select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.portfolio_id == portfolio_id,
                    PortfolioSnapshot.trade_date >= start,
                    PortfolioSnapshot.trade_date <= end,
                    PortfolioSnapshot.complete.is_(True),
                )
                .order_by(PortfolioSnapshot.trade_date)
            )
        )
        .scalars()
        .all()
    )
    if len(snaps) >= 2:
        first_nav, last_nav = snaps[0].nav, snaps[-1].nav
        ret = (last_nav / first_nav - 1) * 100 if first_nav > 0 else 0
        peak = max(s.nav for s in snaps)
        max_dd = (peak - min(s.nav for s in snaps)) / peak * 100 if peak > 0 else 0
        lines.append(f"期间收益：{ret:+.2f}%（净值 {first_nav:,.0f} → {last_nav:,.0f}）")
        lines.append(f"期间最大回撤：{max_dd:.2f}%")
    else:
        lines.append("期间完整快照不足 2 个，无法计算收益与回撤。")

    trades = (
        (
            await session.execute(
                _select(LedgerEntry).where(
                    LedgerEntry.portfolio_id == portfolio_id,
                    LedgerEntry.trade_date >= start,
                    LedgerEntry.trade_date <= end,
                    LedgerEntry.direction != "seed",
                )
            )
        )
        .scalars()
        .all()
    )
    lines.append(f"交易登记：{len(trades)} 笔")

    research = (
        (
            await session.execute(
                _select(ResearchVersion).where(
                    ResearchVersion.user_id == user_id,
                    ResearchVersion.as_of >= start,
                    ResearchVersion.as_of <= end,
                )
            )
        )
        .scalars()
        .all()
    )
    if research:
        from app.services.search_service import names_for

        rnames = await names_for(session, sorted({r.symbol for r in research}))
        parts = sorted({f"{rnames.get(s, s)}({s})" for s in (r.symbol for r in research)})
        lines.append(f"研究更新：{len(research)} 份（{'、'.join(parts)}）")
    else:
        lines.append("研究更新：无")

    events = (
        (
            await session.execute(
                _select(RiskEvent).where(
                    RiskEvent.user_id == user_id,
                    RiskEvent.created_at >= f"{start} 00:00:00",
                )
            )
        )
        .scalars()
        .all()
    )
    red = sum(1 for e in events if e.level == "red")
    yellow = sum(1 for e in events if e.level == "yellow")
    lines.append(f"风险事件：🔴 {red} / 🟡 {yellow}")

    positions = (
        (
            await session.execute(
                _select(Position).where(Position.portfolio_id == portfolio_id).order_by(Position.total_cost.desc())
            )
        )
        .scalars()
        .all()
    )
    if positions:
        from app.services.search_service import names_for

        pnames = await names_for(session, [positions[0].symbol])
        nm = pnames.get(positions[0].symbol)
        title = f"{nm}({positions[0].symbol})" if nm else positions[0].symbol
        lines.append(f"当前第一大持仓（成本口径）：{title} ¥{positions[0].total_cost:,.0f}")
    lines += ["", "ℹ️ 本报告为模板化复盘，不构成投资建议。"]
    return "\n".join(lines)


async def save_report(session: AsyncSession, user_id: int, kind: str, period_label: str, content: str) -> None:
    from sqlalchemy.dialects.mysql import insert as _ins

    from app.storage.models import Report

    stmt = _ins(Report).values(
        user_id=user_id, report_type=kind, period=period_label, content=content
    )
    stmt = stmt.on_duplicate_key_update(content=stmt.inserted.content)
    await session.execute(stmt)
    await session.flush()


async def weekly_report(session: AsyncSession, user_id: int, portfolio) -> str:
    start, end, label = _period_range("weekly")
    content = await build_period_report(session, user_id, portfolio.id, "weekly")
    await save_report(session, user_id, "weekly", label, content)
    return content


async def monthly_report(session: AsyncSession, user_id: int, portfolio) -> str:
    start, end, label = _period_range("monthly")
    content = await build_period_report(session, user_id, portfolio.id, "monthly")
    await save_report(session, user_id, "monthly", label, content)
    return content
