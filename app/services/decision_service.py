"""深度决策卡：结合持仓/财务/研究历史，LLM 对比加仓/持有/减仓。"""
import logging
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.llm import LLMError, chat_json
from app.services.market_context import financial_context, gather_price_context
from app.services.risk_engine import evaluate_portfolio
from app.storage.models import ResearchVersion

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是严谨的 A 股投资决策顾问，服务于个人中长期投资者。不给买卖指令、不承诺收益，最终决策权在用户。

规则：
1. 基于给定的持仓上下文、行情与财务数据、研究历史、组合风险状态做对比分析。
2. 若组合存在集中度超限等风险提示，必须在"加仓"情景的前提条件中说明受限。
3. 财务数据缺失项必须列入 missing_data；定性判断标注"未核验"。
4. 只输出一个严格合法的 JSON 对象。

输出 JSON schema：
{
  "company_name": "公司名称",
  "recommendation": "wait|hold|review_add|review_reduce|insufficient_data",
  "confidence": "high|medium|low",
  "summary": "不超过100字的结论",
  "actions": [
    {"action": "加仓", "rationale": "支持与反对要点", "preconditions": ["前提条件"], "risks": ["风险"]},
    {"action": "持有", "rationale": "...", "preconditions": [...], "risks": [...]},
    {"action": "减仓", "rationale": "...", "preconditions": [...], "risks": [...]}
  ],
  "key_conditions": ["影响后续决策的关键跟踪指标"],
  "missing_data": ["缺失数据"]
}"""


async def run_decision(session: AsyncSession, user_id: int, portfolio_id: int, symbol: str) -> dict[str, Any]:
    price = await gather_price_context(session, symbol, days=250)
    if not price:
        return {"error": f"没有 {symbol} 的行情数据"}

    from sqlalchemy import select as _select

    from app.storage.models import Position

    pos = (
        await session.execute(
            _select(Position).where(Position.portfolio_id == portfolio_id, Position.symbol == symbol)
        )
    ).scalar_one_or_none()
    position_ctx = (
        {"quantity": str(pos.quantity), "avg_cost": str(pos.avg_cost), "total_cost": str(pos.total_cost)}
        if pos
        else {"quantity": "0（当前未持有）"}
    )
    findings, summary = await evaluate_portfolio(session, user_id, portfolio_id, None)
    fin = await financial_context(symbol)
    research = (
        (
            await session.execute(
                _select(ResearchVersion)
                .where(ResearchVersion.user_id == user_id, ResearchVersion.symbol == symbol)
                .order_by(ResearchVersion.version.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    research_ctx = [
        {"version": r.version, "mode": r.mode, "conclusion": r.conclusion, "as_of": r.as_of.isoformat()}
        for r in research
    ]

    user_prompt = (
        f"标的：{symbol}\n"
        f"行情数据：{price}\n"
        f"财务与估值：{fin if fin else '缺失（列入 missing_data）'}\n"
        f"用户当前持仓：{position_ctx}\n"
        f"组合风险提示：{[{'level': f.level, 'code': f.code, 'message': f.message[:80]} for f in findings] or '无'}\n"
        f"历史研究：{research_ctx or '无'}\n\n"
        "请输出决策对比 JSON（加仓/持有/减仓三情景对比，结合用户实际持仓与组合风险）。"
    )
    result = await chat_json(SYSTEM_PROMPT, user_prompt, max_tokens=2500, temperature=0.2)
    valid = _validate_decision(result)
    if valid is None:
        result = await chat_json(
            SYSTEM_PROMPT,
            user_prompt + "\n\n注意：上次输出未通过校验。actions 必须恰好包含 加仓/持有/减仓 三项，recommendation 必须在枚举内。",
            max_tokens=2500,
            temperature=0.0,
        )
        valid = _validate_decision(result)
        if valid is None:
            raise LLMError("决策输出连续两次未通过结构校验")

    count = (
        await session.execute(
            select(func.count(ResearchVersion.id)).where(
                ResearchVersion.user_id == user_id, ResearchVersion.symbol == symbol
            )
        )
    ).scalar()
    version = (count or 0) + 1
    row = ResearchVersion(
        user_id=user_id,
        symbol=symbol,
        version=version,
        mode="decision",
        as_of=date.today(),
        conclusion=valid.get("recommendation"),
        payload=valid,
    )
    session.add(row)
    await session.flush()
    return {"version": version, "payload": valid, "price": price, "fin": fin, "position": position_ctx}


def _validate_decision(r: dict) -> dict | None:
    try:
        if r.get("recommendation") not in ("wait", "hold", "review_add", "review_reduce", "insufficient_data"):
            return None
        actions = r.get("actions")
        if not isinstance(actions, list) or len(actions) < 3:
            return None
        names = {a.get("action") for a in actions}
        if not {"加仓", "持有", "减仓"} <= names:
            return None
        if not isinstance(r.get("summary"), str):
            return None
        return r
    except (TypeError, ValueError):
        return None
