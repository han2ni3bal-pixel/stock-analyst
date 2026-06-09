"""信息储备层 — LLM 加工(P2)。

对「分析窗口内、尚未加工」的事件卡(processed_at 为空)批量调一次 Claude,产出每条的:
- summary    : 一句话中文摘要
- sentiment  : -1..+1 对短期股价的方向(减持/诉讼/下调=负;增持/中标/上调指引=正;中性=0)
- materiality: 0..3 市场影响力(0=噪音/事务性;1=一般;2=较重要;3=重大,如年报/重大合同/并购/业绩预告)

P2 仅基于「标题 + 类型 + 日期」判断(未下载全文,全文解析留 P3)。
一次分析只发一个批量请求(省成本);幂等:只加工 processed_at 空的卡,加工后置位。
复用 sentiment_llm 的 anthropic client 与模型配置。
"""
from __future__ import annotations

import json
import logging
import time

import info_store as store

logger = logging.getLogger(__name__)

try:
    from sentiment_llm import MODEL, _make_client, anthropic
except Exception:  # pragma: no cover
    anthropic = None  # type: ignore

_MARKET_LABEL = {"astock": "A 股", "sh": "A 股", "sz": "A 股", "hk": "港股", "us": "美股"}

_SYSTEM = """你是{label}个股信息面分析师。给你一只股票的一批「公告/备案事件」(仅标题+类型+日期,无正文),
为每条判断它对该股**下一交易日开盘**的潜在影响。注意:看市场反应而非字面,例如
「股东减持/Form 4 内部人卖出」通常为负,「中标大单/上调指引/回购/业绩预增」通常为正,
「召开股东大会通知/薪酬制度/事务性备案」多为中性且影响力低;年报/季报/并购/重大合同影响力高。

严格输出 JSON 数组,每个元素:
{{"idx": 原序号, "summary": "一句话中文摘要", "sentiment": -1..1 的浮点, "materiality": 0|1|2|3}}
只输出 JSON 数组,不要任何前后文字。"""


def enrich_events(conn, cards: list[dict], market: str = "astock", max_items: int = 80) -> int:
    """加工未处理事件卡,回填入库。返回成功加工条数。cards 应已是 processed_at 空的子集。"""
    todo = [c for c in cards if not c.get("processed_at")][:max_items]
    if not todo:
        return 0
    if anthropic is None:
        logger.warning("anthropic SDK 未安装,跳过信息面加工")
        return 0

    items = [{"idx": i, "date": c.get("event_date", ""), "type": c.get("type", ""),
              "title": (c.get("title") or c.get("subtype") or "")[:200]}
             for i, c in enumerate(todo)]
    user_msg = ("以下是同一只股票的 %d 条事件(已按时间倒序):\n\n" % len(items)) + \
        json.dumps(items, ensure_ascii=False, indent=1)
    system = _SYSTEM.format(label=_MARKET_LABEL.get(market, "A 股"))

    client = _make_client()
    resp = None
    last = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=4096,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_msg}],
            )
            break
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:  # type: ignore
            last = e
            if attempt < 2:
                time.sleep(2 ** attempt)
    if resp is None:
        logger.warning("信息面加工 LLM 连接失败(已重试): %s", last)
        return 0

    raw = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:].strip() if raw.startswith("json") else raw
        raw = raw[:-3].strip() if raw.endswith("```") else raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("信息面加工 JSON 解析失败: %s", e)
        return 0

    n = 0
    for r in parsed:
        try:
            c = todo[int(r["idx"])]
        except (KeyError, ValueError, IndexError):
            continue
        sent = r.get("sentiment")
        mat = r.get("materiality")
        store.update_enrichment(
            conn, c["event_id"],
            summary=r.get("summary"),
            key_points=None,
            sentiment=float(sent) if sent is not None else None,
            materiality=int(mat) if mat is not None else None,
        )
        n += 1
    logger.info("信息面加工完成 %d/%d 条", n, len(todo))
    return n
