"""个股研究：LLM 工作流 + 三情景估值 + 版本化保存。"""
import logging
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.llm import LLMError, chat_json
from app.services.market_context import gather_price_context
from app.storage.models import ResearchVersion

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一名严谨的 A 股中长期投资研究员，服务于个人投资研究，不做短线荐股、不给买卖指令、不承诺收益。

规则：
1. 只能基于用户提供的行情数据做确定性陈述；对财务数据缺失的情况，必须在 missing_data 中列明，且估值必须标注"假设性、待财务数据验证"。
2. 你的定性知识（商业模式、行业格局）作为线索提供，必须在 quality_flags.llm_knowledge_unverified=true 时明确说明未经核验。
3. 结论只能是枚举值之一，不得含糊。
4. 反方观点必须至少 2 条，投资逻辑必须至少 2 条。
5. 情景价格必须为正数，且悲观 < 中性 < 乐观。
6. 只输出一个严格合法的 JSON 对象，无任何其他文字。

输出 JSON schema：
{
  "company_name": "公司名称",
  "conclusion": "wait|research_entry|hold|review_add|review_reduce|review_exit|insufficient_data",
  "summary": "不超过120字的结论摘要",
  "thesis": [{"point": "投资逻辑要点", "confidence": "high|medium|low"}],
  "counterarguments": ["反方观点"],
  "risks": ["主要风险"],
  "invalidation_conditions": ["投资逻辑失效条件"],
  "valuation": {
    "method": "估值方法说明",
    "assumptions": ["关键假设"],
    "warning": "估值局限说明",
    "scenarios": {
      "pessimistic": {"price": 0, "rationale": "简述"},
      "neutral": {"price": 0, "rationale": "简述"},
      "optimistic": {"price": 0, "rationale": "简述"}
    }
  },
  "missing_data": ["缺失的关键数据"],
  "quality_flags": {"financial_data_available": false, "llm_knowledge_unverified": true}
}"""

CONCLUSION_LABELS = {
    "wait": "观望",
    "research_entry": "可进入观察清单",
    "hold": "持有观察",
    "review_add": "评估加仓",
    "review_reduce": "评估减仓",
    "review_exit": "评估退出",
    "insufficient_data": "数据不足，无法下结论",
}


def _validate(result: dict) -> dict | None:
    """最小结构校验；不合法返回 None。"""
    try:
        c = result["conclusion"]
        if c not in CONCLUSION_LABELS:
            return None
        sc = result["valuation"]["scenarios"]
        p, n, o = sc["pessimistic"]["price"], sc["neutral"]["price"], sc["optimistic"]["price"]
        if not (0 < p < n < o):
            return None
        if not isinstance(result.get("thesis"), list) or len(result["thesis"]) < 2:
            return None
        if not isinstance(result.get("counterarguments"), list) or len(result["counterarguments"]) < 2:
            return None
        return result
    except (KeyError, TypeError, ValueError):
        return None


async def run_research(session: AsyncSession, user_id: int, symbol: str, mode: str = "research") -> dict[str, Any]:
    """完整研究流程：行情上下文 → LLM → 校验 → 版本化保存。"""
    price = await gather_price_context(session, symbol, days=250)
    if not price:
        return {"error": f"没有 {symbol} 的行情数据（代码可能无效或不支持的市场）"}
    from app.services.market_context import financial_context

    fin = await financial_context(symbol)

    mode_note = (
        "本次任务：完整个股研究（投资逻辑、反方、风险、三情景估值）。"
        if mode == "research"
        else "本次任务：重点输出三情景估值及关键假设，thesis 与 counterarguments 各给 2 条即可。"
    )
    if fin:
        fin_note = (
            "财务与估值数据（来源：东方财富公开快照与F10主要指标，未独立核验；"
            "分红历史、现金流量明细等仍缺失，需列入 missing_data）：\n"
            f"{fin}\n\n"
        )
    else:
        fin_note = "财务数据：本次未获取到财务数据（missing_data 必须包含财报关键项）。\n\n"
    user_prompt = (
        f"{mode_note}\n\n"
        f"标的：{symbol}\n"
        f"行情数据（来源：腾讯公开日K，未复权收盘，JSON）：\n"
        f"{price}\n\n"
        f"{fin_note}"
        "请基于给定数据与你的公开知识（定性内容标注未核验）输出 JSON。"
    )
    result = await chat_json(SYSTEM_PROMPT, user_prompt, max_tokens=3000)
    validated = _validate(result)
    if validated is None:
        result = await chat_json(
            SYSTEM_PROMPT,
            user_prompt + "\n\n注意：上一次输出未通过结构校验。请严格按 schema 输出，情景价格必须 0 < 悲观 < 中性 < 乐观。",
            max_tokens=3000,
        )
        validated = _validate(result)
        if validated is None:
            raise LLMError("研究输出连续两次未通过结构校验")

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
        mode=mode,
        as_of=date.today(),
        conclusion=validated["conclusion"],
        payload=validated,
    )
    qf = validated.setdefault("quality_flags", {})
    qf["financial_data_available"] = bool(fin)
    qf["llm_knowledge_unverified"] = True
    session.add(row)
    await session.flush()
    return {"version": version, "payload": validated, "price": price, "fin": fin}
