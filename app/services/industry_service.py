"""行业周期雷达 v1：代表股篮子确定性统计 + LLM 定性周期判断（明确标注未核验）。

设计中的供需/库存/产能数据源尚未接入，本轮为"篮子动量 + 模型知识"降级实现。
"""
import asyncio
from dataclasses import dataclass
from typing import Any

from app.providers.llm import LLMError, chat_json
from app.services.market_context import gather_price_context
from app.storage.db import Session

INDUSTRY_MAP: dict[str, list[str]] = {
    "煤炭": ["601088.SH", "601225.SH", "600188.SH", "601898.SH", "601015.SH"],
    "银行": ["601398.SH", "601939.SH", "600036.SH", "601166.SH", "600016.SH"],
    "白酒": ["600519.SH", "000858.SZ", "000568.SZ", "600809.SH", "000596.SZ"],
    "房地产": ["000002.SZ", "600048.SH", "600606.SH", "001979.SZ", "600383.SH"],
    "钢铁": ["600019.SH", "000709.SZ", "600010.SH", "002110.SZ", "000898.SZ"],
    "有色": ["601899.SH", "601600.SH", "600111.SH", "603993.SH", "000060.SZ"],
    "电力": ["600900.SH", "601016.SH", "600021.SH", "003816.SZ", "600886.SH"],
    "医药": ["600276.SH", "000538.SZ", "601607.SH", "000661.SZ", "600436.SH"],
    "半导体": ["688981.SH", "002371.SZ", "603986.SH", "002049.SZ", "688012.SH"],
    "新能源车": ["300750.SZ", "002594.SZ", "601127.SH", "600066.SH", "002460.SZ"],
}


@dataclass
class BasketStats:
    industry: str
    stocks: list[dict[str, Any]]


def supported_industries() -> list[str]:
    return list(INDUSTRY_MAP.keys())


async def scan_industry(industry: str) -> dict[str, Any] | str:
    symbols = INDUSTRY_MAP.get(industry)
    if not symbols:
        return f"暂不支持行业「{industry}」。支持：{'、'.join(supported_industries())}"

    async def one(sym: str) -> dict[str, Any]:
        async with Session() as session:
            ctx = await gather_price_context(session, sym, days=420)
        return {"symbol": sym, **(ctx or {})}

    stocks = await asyncio.gather(*(one(s) for s in symbols))
    stocks = [s for s in stocks if s.get("last_close") is not None]
    if not stocks:
        return f"行业「{industry}」代表股行情获取失败"

    def avg(key: str) -> float | None:
        vals = [s[key] for s in stocks if s.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    basket = {
        "industry": industry,
        "avg_chg_60d_pct": avg("chg_60d_pct"),
        "avg_chg_120d_pct": avg("chg_120d_pct"),
        "avg_pos_in_250d_range_pct": round(
            sum(
                (s["last_close"] - s["low_250d"]) / (s["high_250d"] - s["low_250d"]) * 100
                for s in stocks
                if s.get("high_250d") and s["high_250d"] > s["low_250d"]
            )
            / len(stocks),
            1,
        ),
        "stocks": [
            {
                "symbol": s["symbol"],
                "chg_60d_pct": s.get("chg_60d_pct"),
                "pos_in_range_pct": (
                    round((s["last_close"] - s["low_250d"]) / (s["high_250d"] - s["low_250d"]) * 100, 1)
                    if s.get("high_250d") and s["high_250d"] > s["low_250d"]
                    else None
                ),
            }
            for s in stocks
        ],
    }

    SYSTEM = """你是行业周期研究员。基于给出的代表股篮子统计（确定性数据）与你的行业知识，输出行业周期定性判断。
必须声明：你的供需/库存/产能判断来自模型知识，未经实时数据核验。
只输出严格 JSON：
{
  "cycle_position": "底部|回升|中位|高位|回落",
  "supply_demand_view": "供需格局定性描述（无数据时明确说缺乏核验）",
  "earnings_trend_view": "盈利趋势定性判断",
  "risks": ["行业主要风险"],
  "watch_points": ["建议跟踪的公开数据（如官方库存、行业开工率、价格指数）"],
  "summary": "不超过100字总结"
}"""
    result = await chat_json(SYSTEM, f"行业：{industry}\n篮子统计：{basket}", max_tokens=1500, temperature=0.2)
    if not isinstance(result, dict) or "cycle_position" not in result:
        raise LLMError("行业判断输出异常")
    return {"basket": basket, "qualitative": result}
