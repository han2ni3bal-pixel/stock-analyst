"""Optional, provider-neutral LLM gateway for stock-analyst.

Core market analysis must work without an LLM.  Callers should inspect
``get_llm_status()`` and treat an unavailable provider as a skipped optional
enhancement, not as an analysis failure.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any


_DISABLED = {"0", "disabled", "none", "off", "false", "no"}


class LLMError(RuntimeError):
    """A configured LLM provider failed to complete a request."""


class LLMUnavailableError(LLMError):
    """No usable optional LLM provider is configured."""


@dataclass(frozen=True)
class LLMStatus:
    provider: str
    available: bool
    model: str = ""
    reason: str = ""


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


def get_llm_status() -> LLMStatus:
    """Resolve the selected provider without making a network call."""
    requested = os.getenv("STOCK_ANALYST_LLM_PROVIDER", "auto").strip().lower()
    if requested in _DISABLED:
        return LLMStatus("disabled", False, reason="LLM 增强已关闭")

    if requested == "auto":
        if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
            requested = "anthropic"
        elif os.getenv("OPENAI_API_KEY") or os.getenv("STOCK_ANALYST_LLM_API_KEY"):
            requested = "openai"
        else:
            return LLMStatus("disabled", False, reason="未配置 LLM 凭证，已跳过可选增强")

    if requested in {"anthropic", "claude"}:
        if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
            return LLMStatus("anthropic", False, reason="未配置 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN")
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return LLMStatus("anthropic", False, reason="anthropic SDK 未安装")
        model = (os.getenv("STOCK_ANALYST_LLM_MODEL")
                 or os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL")
                 or "claude-opus-4-7")
        return LLMStatus("anthropic", True, model=model)

    if requested in {"openai", "openai-compatible", "openai_compatible"}:
        key = os.getenv("STOCK_ANALYST_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        model = os.getenv("STOCK_ANALYST_LLM_MODEL") or os.getenv("OPENAI_MODEL", "")
        if not key:
            return LLMStatus("openai", False, model=model, reason="未配置 OPENAI_API_KEY")
        if not model:
            return LLMStatus("openai", False, reason="未配置 STOCK_ANALYST_LLM_MODEL / OPENAI_MODEL")
        return LLMStatus("openai", True, model=model)

    return LLMStatus(requested or "unknown", False, reason=f"不支持的 LLM provider: {requested}")


def _http_client(timeout_seconds: float):
    import httpx

    proxy = (os.getenv("STOCK_ANALYST_LLM_HTTP_PROXY")
             or os.getenv("ANTHROPIC_HTTP_PROXY") or "").strip()
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)
    if proxy:
        return httpx.Client(proxy=proxy, trust_env=False, timeout=timeout)
    return httpx.Client(trust_env=False, timeout=timeout)


def generate_text(
    user_prompt: str,
    *,
    system_prompt: str = "",
    max_tokens: int = 4096,
    timeout_seconds: float = 120.0,
    retries: int = 2,
    thinking: bool = False,
) -> LLMResponse:
    """Generate text through the configured provider."""
    status = get_llm_status()
    if not status.available:
        raise LLMUnavailableError(status.reason)

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            if status.provider == "anthropic":
                return _generate_anthropic(
                    status, user_prompt, system_prompt, max_tokens,
                    timeout_seconds, thinking,
                )
            if status.provider == "openai":
                return _generate_openai(
                    status, user_prompt, system_prompt, max_tokens,
                    timeout_seconds,
                )
            raise LLMUnavailableError(status.reason)
        except LLMUnavailableError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max(1, retries):
                time.sleep(2 ** attempt)

    raise LLMError(f"{status.provider} 调用失败（已重试 {max(1, retries)} 次）：{last_error}")


def _generate_anthropic(
    status: LLMStatus,
    user_prompt: str,
    system_prompt: str,
    max_tokens: int,
    timeout_seconds: float,
    thinking: bool,
) -> LLMResponse:
    import anthropic

    with _http_client(timeout_seconds) as http_client:
        client = anthropic.Anthropic(http_client=http_client)
        kwargs: dict[str, Any] = {
            "model": status.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        response = client.messages.create(**kwargs)

    text = "\n".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip()
    usage = {
        "input_tokens": int(getattr(response.usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(response.usage, "output_tokens", 0) or 0),
        "cache_read": int(getattr(response.usage, "cache_read_input_tokens", 0) or 0),
        "cache_create": int(getattr(response.usage, "cache_creation_input_tokens", 0) or 0),
    }
    return LLMResponse(text=text, provider=status.provider, model=status.model, usage=usage)


def _generate_openai(
    status: LLMStatus,
    user_prompt: str,
    system_prompt: str,
    max_tokens: int,
    timeout_seconds: float,
) -> LLMResponse:
    base = (os.getenv("STOCK_ANALYST_LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    endpoint = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    key = os.getenv("STOCK_ANALYST_LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    payload = {"model": status.model, "messages": messages, "max_tokens": max_tokens}

    with _http_client(timeout_seconds) as http_client:
        response = http_client.post(
            endpoint,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    text = data["choices"][0]["message"]["content"]
    if isinstance(text, list):
        text = "\n".join(str(part.get("text", "")) for part in text if isinstance(part, dict))
    raw_usage = data.get("usage") or {}
    usage = {
        "input_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
    }
    return LLMResponse(text=str(text).strip(), provider=status.provider, model=status.model, usage=usage)


def parse_json_text(text: str) -> Any:
    """Parse a JSON object/array, tolerating Markdown code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
        if not starts:
            raise
        value, _ = decoder.raw_decode(cleaned[min(starts):])
        return value
