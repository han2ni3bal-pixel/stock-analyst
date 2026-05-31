"""基于 Claude API 的新闻情感分析 — 自动事件去重 / 重要性分级 / 时效性。"""
from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd

try:
    import anthropic
    import httpx
except ImportError:
    anthropic = None
    httpx = None


def _make_client():
    """构造 anthropic.Client，绕过系统代理。

    Why: ANTHROPIC_BASE_URL 经常指向内部网关（如 *.ai.srv / 公司内网 LLM 路由），
    若 macOS 开了系统 HTTP 代理，httpx 会自动把请求转给代理，导致内部域名被转出
    后断连（RemoteProtocolError）。这里强制 trust_env=False 阻断 env / 系统代理探测，
    并设更短的连接超时方便排错。如果用户确实需要走代理访问 Anthropic，自己设
    ANTHROPIC_HTTP_PROXY 即可。
    """
    explicit_proxy = os.getenv("ANTHROPIC_HTTP_PROXY", "").strip()
    timeout = httpx.Timeout(120.0, connect=10.0)
    if explicit_proxy:
        http_client = httpx.Client(proxy=explicit_proxy, trust_env=False, timeout=timeout)
    else:
        http_client = httpx.Client(trust_env=False, timeout=timeout)
    return anthropic.Anthropic(http_client=http_client)

MODEL = os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-4-7")


SYSTEM_PROMPT_TEMPLATE = """你是{market_label}个股舆情分析师。给你一组关于某只股票的新闻条目（可能为中文或英文），你的任务：

1. **事件去重**：同一事件被多家媒体报道时合并为一条，不重复计分。
2. **重要性分级**：每个独立事件按 1-5 打分（1=噪音/重复转载；3=常规新闻；5=重大利好/利空，如业绩预告、并购、监管动作、大订单、减持公告、产品发布、指引变更等）。
3. **方向判定**：每个事件标记 +/- 或 中性。重点看市场可能的反应而非字面褒贬，例如"减持/internal selling"通常是 -，"raised guidance / 中标大订单"通常是 +。{market_hint}
4. **时效性**：标记是 (a) 已发生且市场可能消化 (b) 即将兑现 (c) 远期催化 / 不确定。
5. **整体结论**：给出对短期（下一交易日开盘）走势的影响判断 — bullish / neutral / bearish 之一，及置信度 (low/medium/high)，并简述核心理由。

严格按 JSON 格式回答，键为：
{{
  "events": [
    {{"summary": "事件简述（用中文）", "direction": "+|-|0", "importance": 1-5, "timing": "past|near|far", "sources_count": N}}
  ],
  "verdict": "bullish|neutral|bearish",
  "confidence": "low|medium|high",
  "score": -5.0 到 +5.0 的浮点数,
  "rationale": "简短中文解释，重点是哪些事件主导了判断"
}}

只输出 JSON，不要任何前后文字。"""


_MARKET_LABEL = {
    "astock": "A 股",
    "hk":     "港股",
    "us":     "美股",
    "etf":    "ETF",
}

_MARKET_HINT = {
    "us": "美股需特别关注：财报指引（guidance）、analyst rating 升降级、SEC 调查、产品/订单 catalysts、buyback / 分拆公告、关键人员变动、宏观联储路径对成长股的传导。",
    "hk": "港股需关注：南向资金倾向、港交所披露易（13.07 / 14A.95 等）公告、AH 价差、人民币汇率与香港联汇制度对资金面的影响。",
}


def _build_system_prompt(market: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        market_label=_MARKET_LABEL.get(market, "A 股"),
        market_hint=_MARKET_HINT.get(market, ""),
    )


def analyze_news_with_llm(
    news_df: pd.DataFrame,
    stock_name: str,
    target_date: str,
    market: str = "astock",
    title_col: str | None = None,
    content_col: str | None = None,
    time_col: str | None = None,
    max_items: int = 60,
) -> dict[str, Any]:
    if anthropic is None:
        return {"error": "anthropic SDK 未安装", "verdict": "neutral", "score": 0, "events": []}

    if news_df is None or news_df.empty:
        return {"verdict": "neutral", "score": 0, "events": [], "rationale": "无新闻"}

    title_col = title_col or next(
        (c for c in news_df.columns if "标题" in c or c.lower() in ("title", "headline")), None
    )
    content_col = content_col or next(
        (c for c in news_df.columns if "内容" in c or c.lower() in ("content", "summary", "description")), None
    )
    time_col = time_col or next(
        (c for c in news_df.columns if "时间" in c or c.lower() in ("date", "time", "pubdate", "published")), None
    )

    items = []
    for _, row in news_df.head(max_items).iterrows():
        item = {}
        if time_col:
            item["time"] = str(row.get(time_col, ""))
        if title_col:
            item["title"] = str(row.get(title_col, ""))[:200]
        if content_col:
            item["content"] = str(row.get(content_col, ""))[:500]
        items.append(item)

    user_msg = (
        f"股票：{stock_name}\n"
        f"分析目标日：{target_date}\n"
        f"以下是 {len(items)} 条相关新闻（已按时间倒序）：\n\n"
        + json.dumps(items, ensure_ascii=False, indent=1)
    )

    client = _make_client()
    last_exc: Exception | None = None
    resp = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                system=[{
                    "type": "text",
                    "text": _build_system_prompt(market),
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_msg}],
            )
            break
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            last_exc = e
            if attempt < 2:
                import time
                time.sleep(2 ** attempt)
            continue
    if resp is None:
        return {
            "error": f"Anthropic API 连接失败（已重试 3 次）: {last_exc}",
            "verdict": "neutral",
            "score": 0,
            "events": [],
        }

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    raw = "\n".join(text_parts).strip()

    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            "error": f"模型输出无法解析为 JSON: {e}",
            "raw": raw[:500],
            "verdict": "neutral",
            "score": 0,
            "events": [],
        }

    parsed["_usage"] = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0),
        "cache_create": getattr(resp.usage, "cache_creation_input_tokens", 0),
    }
    return parsed
