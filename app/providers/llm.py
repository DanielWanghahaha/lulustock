"""LLM 客户端：OpenAI 兼容接口（DeepSeek/Qwen/GLM/Kimi 等），JSON 模式。"""
import json
import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


async def chat_json(
    system: str,
    user: str,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    retries: int = 1,
) -> dict:
    """调用 chat/completions 并解析 JSON 对象；失败重试一次更严格的指令。"""
    settings = get_settings()
    if not settings.llm_api_key:
        raise LLMError("LLM_API_KEY 未配置")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    body = {
        "model": settings.llm_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(url, headers=headers, json=body)
                if resp.status_code == 402:
                    raise LLMError("LLM 账户余额不足，请充值或更换供应商")
                if resp.status_code == 401:
                    raise LLMError("LLM API Key 无效")
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
            try:
                return json.loads(content)
            except json.JSONDecodeError as exc:
                last_exc = exc
                body["messages"] = messages + [
                    {"role": "assistant", "content": content[:500]},
                    {"role": "user", "content": "上面的输出不是合法 JSON。请重新只输出一个合法 JSON 对象，不要任何其他文字。"},
                ]
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise LLMError(f"LLM 调用失败: {last_exc}")
