from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from openai import OpenAI

from . import settings
from .schemas import JudgeModelConfig, JudgeVerdict


@dataclass
class LLMCallResult:
    text: str
    latency_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


# Грубые ориентиры USD за 1M токенов (подставьте свои под реальный прайс judge-модели)
DEFAULT_PRICE_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "grok": (2.0, 10.0),
    "x-ai": (2.0, 10.0),
    "default": (0.2, 0.2),
}


def _estimate_cost(model: str, prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    if prompt_tokens is None and completion_tokens is None:
        return None
    p = prompt_tokens or 0
    c = completion_tokens or 0
    key = next((k for k in DEFAULT_PRICE_PER_1M if k in model.lower()), "default")
    pin, pout = DEFAULT_PRICE_PER_1M[key]
    return (p / 1_000_000) * pin + (c / 1_000_000) * pout


def build_client(cfg: JudgeModelConfig) -> OpenAI:
    key = os.getenv(cfg.api_key_env, "")
    # getenv на каждый вызов: после load_dotenv(extra_env_file) в runner подхватятся актуальные XAI_*
    base = cfg.base_url or os.getenv("XAI_BASE_URL", settings.DEFAULT_XAI_BASE_URL)
    return OpenAI(api_key=key, base_url=base)


def complete_chat(
    client: OpenAI,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    _retry_sleep: int = 30,
) -> LLMCallResult:
    while True:
        t0 = time.perf_counter()
        try:
            # Как в academiy_test_llm_safety/backend/app/llm.py: Grok через OpenAI SDK без response_format
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
        except Exception as e:
            if getattr(e, "status_code", None) == 429 or "429" in str(e):
                logger.warning(f"429 rate limit for {model}, sleeping {_retry_sleep}s...")
                time.sleep(_retry_sleep)
                continue
            raise
        latency_ms = (time.perf_counter() - t0) * 1000
        text = resp.choices[0].message.content or "{}"
        u = resp.usage
        pt = u.prompt_tokens if u else None
        ct = u.completion_tokens if u else None
        tt = u.total_tokens if u else None
        return LLMCallResult(text, latency_ms, pt, ct, tt)


def _strip_markdown_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def parse_verdict_json(text: str, task_type: str) -> JudgeVerdict:
    """Ожидаем JSON с полями unsafe (bool), confidence (0-1), explanation (str)."""
    text = _strip_markdown_fence(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        data = json.loads(m.group(0)) if m else {}

    unsafe = data.get("unsafe")
    if unsafe is None:
        unsafe = data.get("pred_unsafe", data.get("is_unsafe", False))
    conf = float(data.get("confidence", data.get("score", 0.5)))
    conf = max(0.0, min(1.0, conf))
    expl = str(data.get("explanation", data.get("reason", "")))
    return JudgeVerdict(pred_unsafe=bool(unsafe), confidence=conf, explanation=expl, raw_json=data)


def verdict_for_task(task_type: str, unsafe: bool) -> bool:
    """unsafe в JSON трактуем как «положительный класс риска» для всех task_type."""
    return unsafe
