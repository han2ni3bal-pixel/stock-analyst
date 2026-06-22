"""新闻情感分析。

LLM 可用时优先用 LLM；未配置或调用失败时，自动回退到可解释的本地规则模型。
本地模型覆盖事件去重、方向词加权、否定词处理、重要性和时效衰减，保证离线报告
仍能展示新闻事件及基础情感判断。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

import pandas as pd

from llm_client import LLMError, generate_text, get_llm_status, parse_json_text


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


# 词组权重刻意保持稀疏：优先识别会改变盈利、资本结构或监管风险的事件，
# 避免把普通宣传稿里的“领先”“赋能”等措辞误判为强利好。
_POSITIVE_TERMS = {
    "业绩预增": 3.0, "扭亏为盈": 3.0, "大幅增长": 2.0, "同比增长": 1.0,
    "超预期": 2.5, "中标": 2.0, "重大合同": 2.5, "签订合同": 2.0,
    "回购": 2.0, "增持": 2.0, "上调评级": 2.0, "获批": 2.0,
    "扩产": 1.0, "投产": 1.0, "战略合作": 1.0, "分红": 1.0,
    "创新高": 0.5, "涨停": 0.5,
    "beats estimates": 2.5, "raised guidance": 3.0, "upgrade": 2.0,
    "buyback": 2.0, "contract win": 2.0, "record revenue": 2.0,
}

_NEGATIVE_TERMS = {
    "业绩预减": -3.0, "预亏": -3.0, "亏损": -2.0, "同比下降": -2.0,
    "大幅下滑": -2.5, "下滑": -1.5, "减持": -2.0, "立案": -3.0,
    "调查": -2.0, "处罚": -3.0, "问询函": -1.5, "监管函": -2.0,
    "终止": -2.0, "违约": -3.0, "诉讼": -2.0, "风险提示": -1.0,
    "异动公告": -0.5, "跌停": -1.0, "商誉减值": -2.5, "资产减值": -2.0,
    "misses estimates": -2.5, "cut guidance": -3.0, "downgrade": -2.0,
    "investigation": -2.5, "lawsuit": -2.0, "offering": -1.0,
}

_MATERIAL_TERMS = (
    "业绩", "利润", "营收", "财报", "中标", "合同", "订单", "并购", "收购",
    "重组", "回购", "增持", "减持", "立案", "调查", "处罚", "问询", "监管",
    "诉讼", "违约", "减值", "获批", "guidance", "earnings", "contract",
    "buyback", "investigation", "lawsuit",
)

_NEGATION_RE = re.compile(r"(?:不|未|无|否认|不存在|没有|not|no).{0,6}$", re.I)


def _find_column(df: pd.DataFrame, needles: tuple[str, ...], exact: tuple[str, ...]) -> str | None:
    for col in df.columns:
        label = str(col)
        if any(needle in label for needle in needles) or label.lower() in exact:
            return col
    return None


def _prepare_items(
    news_df: pd.DataFrame,
    title_col: str | None,
    content_col: str | None,
    time_col: str | None,
    max_items: int,
) -> list[dict[str, str]]:
    title_col = title_col or _find_column(news_df, ("标题",), ("title", "headline"))
    content_col = content_col or _find_column(
        news_df, ("内容", "摘要"), ("content", "summary", "description")
    )
    time_col = time_col or _find_column(
        news_df, ("时间", "日期"), ("date", "time", "pubdate", "published")
    )
    source_col = _find_column(news_df, ("来源", "媒体"), ("source", "publisher"))

    items: list[dict[str, str]] = []
    for _, row in news_df.head(max_items).iterrows():
        title = str(row.get(title_col, "") if title_col else "").strip()
        content = str(row.get(content_col, "") if content_col else "").strip()
        if not title and not content:
            continue
        items.append({
            "time": str(row.get(time_col, "") if time_col else ""),
            "title": title[:200],
            "content": content[:800],
            "source": str(row.get(source_col, "") if source_col else "")[:80],
        })
    return items


def _normalise_title(title: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", title).lower()


def _deduplicate_items(items: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for item in items:
        key = _normalise_title(item["title"] or item["content"][:80])
        matched = None
        for group in groups:
            other = group["_key"]
            similar = key and other and (
                key in other or other in key or SequenceMatcher(None, key, other).ratio() >= 0.72
            )
            if similar:
                matched = group
                break
        if matched is None:
            groups.append({**item, "_key": key, "sources_count": 1})
        else:
            matched["sources_count"] += 1
            if len(item["content"]) > len(matched["content"]):
                matched["content"] = item["content"]
            if not matched.get("source") and item.get("source"):
                matched["source"] = item["source"]
    return groups


def _term_score(text: str) -> tuple[float, list[str]]:
    lowered = text.lower()
    score = 0.0
    hits: list[str] = []
    for term, weight in {**_POSITIVE_TERMS, **_NEGATIVE_TERMS}.items():
        start = 0
        while True:
            idx = lowered.find(term.lower(), start)
            if idx < 0:
                break
            prefix = lowered[max(0, idx - 12):idx]
            # “不存在减持”“未发生亏损”等否定表达不按原方向计分。
            if not _NEGATION_RE.search(prefix):
                score += weight
                hits.append(term)
            start = idx + len(term)
    return max(-5.0, min(5.0, score)), hits


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


def _timing_and_decay(item: dict[str, Any], target_date: str) -> tuple[str, float]:
    text = f"{item.get('title', '')} {item.get('content', '')}".lower()
    if re.search(r"拟|计划|预计|未来|有望|可能|proposal|plans? to|expected", text):
        return "far", 0.55
    dt = _parse_time(str(item.get("time", "")))
    try:
        target = datetime.strptime(target_date[:10], "%Y-%m-%d")
    except ValueError:
        target = datetime.now()
    if dt is None:
        return "past", 0.65
    age = (target.date() - dt.date()).days
    if age <= 2:
        return "near", 1.0
    if age <= 7:
        return "past", 0.8
    return "past", 0.5


def analyze_news_locally(
    news_df: pd.DataFrame,
    stock_name: str,
    target_date: str,
    market: str = "astock",
    title_col: str | None = None,
    content_col: str | None = None,
    time_col: str | None = None,
    max_items: int = 60,
    fallback_reason: str = "",
) -> dict[str, Any]:
    """无需外部模型的可解释新闻情感降级。"""
    if news_df is None or news_df.empty:
        return {"available": False, "verdict": "neutral", "confidence": "low",
                "score": 0, "events": [], "rationale": "无新闻"}

    raw_items = _prepare_items(news_df, title_col, content_col, time_col, max_items)
    groups = _deduplicate_items(raw_items)
    events: list[dict[str, Any]] = []
    numerator = 0.0
    denominator = 0.0
    directional = 0

    for item in groups:
        text = f"{item.get('title', '')}。{item.get('content', '')}"
        raw_score, hits = _term_score(text)
        direction = "+" if raw_score >= 0.5 else ("-" if raw_score <= -0.5 else "0")
        material_hits = sum(1 for term in _MATERIAL_TERMS if term in text.lower())
        importance = min(5, max(1, 1 + int(math.ceil(abs(raw_score))) + min(2, material_hits)))
        timing, decay = _timing_and_decay(item, target_date)
        strength = max(-1.0, min(1.0, raw_score / 3.0))
        numerator += strength * importance * decay
        denominator += importance
        if direction != "0":
            directional += 1
        summary = item.get("title") or item.get("content", "")[:120]
        events.append({
            "summary": summary,
            "direction": direction,
            "importance": importance,
            "timing": timing,
            "sources_count": int(item.get("sources_count", 1)),
            "matched_terms": hits[:8],
        })

    score = round(max(-5.0, min(5.0, 5.0 * numerator / denominator)), 2) if denominator else 0.0
    verdict = "bullish" if score >= 0.75 else ("bearish" if score <= -0.75 else "neutral")
    if directional >= 4 and abs(score) >= 2.0:
        confidence = "high"
    elif directional >= 2 and abs(score) >= 0.75:
        confidence = "medium"
    else:
        confidence = "low"

    events.sort(key=lambda e: (e["importance"], e["direction"] != "0"), reverse=True)
    positive = sum(e["direction"] == "+" for e in events)
    negative = sum(e["direction"] == "-" for e in events)
    rationale = (
        f"本地规则模型分析 {len(raw_items)} 条新闻、合并为 {len(events)} 个事件；"
        f"识别正面 {positive} 个、负面 {negative} 个。"
        "结果依据事件关键词、重要性及发布时间衰减，仅作为无 LLM 时的基础判断。"
    )
    result: dict[str, Any] = {
        "available": True,
        "verdict": verdict,
        "confidence": confidence,
        "score": score,
        "rationale": rationale,
        "events": events[:12],
        "_provider": "local-rules",
        "_model": "sentiment-lexicon-v1",
        "_news_count": len(raw_items),
        "_event_count": len(events),
    }
    if fallback_reason:
        result["_fallback_reason"] = fallback_reason
    return result


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
    status = get_llm_status()
    if not status.available:
        return analyze_news_locally(
            news_df, stock_name, target_date, market, title_col, content_col,
            time_col, max_items, fallback_reason=status.reason,
        )

    if news_df is None or news_df.empty:
        return {"verdict": "neutral", "score": 0, "events": [], "rationale": "无新闻"}

    items = _prepare_items(news_df, title_col, content_col, time_col, max_items)

    user_msg = (
        f"股票：{stock_name}\n"
        f"分析目标日：{target_date}\n"
        f"以下是 {len(items)} 条相关新闻（已按时间倒序）：\n\n"
        + json.dumps(items, ensure_ascii=False, indent=1)
    )

    try:
        response = generate_text(
            user_msg,
            system_prompt=_build_system_prompt(market),
            max_tokens=4096,
            retries=3,
            thinking=True,
        )
    except LLMError as exc:
        return analyze_news_locally(
            news_df, stock_name, target_date, market, title_col, content_col,
            time_col, max_items, fallback_reason=f"LLM 调用失败: {exc}",
        )

    try:
        parsed = parse_json_text(response.text)
    except (json.JSONDecodeError, ValueError) as e:
        return analyze_news_locally(
            news_df, stock_name, target_date, market, title_col, content_col,
            time_col, max_items, fallback_reason=f"LLM 输出解析失败: {e}",
        )

    parsed["available"] = True
    parsed["_provider"] = response.provider
    parsed["_model"] = response.model
    parsed["_usage"] = response.usage
    return parsed
