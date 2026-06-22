"""板块联动 — 同行业看多标的扫描器。

流程：
1. 给定目标股票，调用 Claude 推荐 A 股 + 美股两个市场的同行业热门 peers
2. 对每个 peer 跑轻量分析（K 线 + 技术指标 + 当日涨跌）→ 得到信号分 / verdict
3. 过滤"看涨且置信度 ≥ 中等"的标的，按信号分降序，取 top N
4. 生成简约分析（一句话）

注意：peers 不跑新闻情感（成本太高），仅靠技术面 + 价格行为出 verdict。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd

from data_layer import fetch_kline
from llm_client import LLMError, generate_text, get_llm_status, parse_json_text
from technical import compute_indicators, analyze_signals as analyze_tech


# ---------------- LLM 推荐 peers ----------------

_PEER_SYSTEM_PROMPT = """你是行业研究员。给定一只股票（A 股 / 港股 / 美股 / ETF），列出该股票所在行业中**最值得关注、流动性最好**的上市公司。

返回严格 JSON：
{
  "industry_zh": "中文行业名（如：半导体、新能源车、AI 芯片、消费电子）",
  "industry_en": "英文 sector 名（如：Semiconductors, EV, AI Infrastructure）",
  "a_stock_peers": [
    {"code": "603501", "name": "韦尔股份", "reason": "国产 CIS 龙头"}
  ],
  "us_stock_peers": [
    {"ticker": "NVDA", "name": "Nvidia", "reason": "AI GPU 龙头"}
  ]
}

规则：
- A 股 8 只：6 位代码，仅沪深主板 / 创业板 / 科创板。**严禁包含北交所代码（4xx / 8xx 开头）**
- 美股 8 只：1-5 字母 ticker，全大写
- 优先大流动性龙头，避免冷门小票
- 不要包含目标股票自身
- reason 用一句话讲为什么是同行业核心股
- 跨市场配对：尽量从 A 股/美股两边都选出确实同业的对标（如目标是英伟达 → A股选寒武纪/海光/景嘉微，美股选 AMD/TSM/AVGO）

只输出 JSON，不要任何额外文字。"""


def _suggest_peers(target_name: str, target_code: str, target_market: str,
                   industry_hint: str | None) -> dict:
    status = get_llm_status()
    if not status.available:
        return {}

    user_msg = (
        f"目标股票：{target_name}（代码 {target_code}，市场 {target_market}）\n"
        f"已知行业：{industry_hint or '未提供，请你自行判断'}\n"
        "请列出该行业最热门的同行 peers（A 股 8 只 + 美股 8 只）。"
    )

    try:
        response = generate_text(
            user_msg,
            system_prompt=_PEER_SYSTEM_PROMPT,
            max_tokens=2048,
            timeout_seconds=60,
            retries=2,
        )
        result = parse_json_text(response.text)
        return result if isinstance(result, dict) else {}
    except (LLMError, ValueError, TypeError) as exc:
        print(f"  [Peer] LLM 推荐失败: {exc}")
        return {}


# ---------------- 单只 peer 的轻量信号 ----------------

def _market_for_a(code: str) -> str:
    if code.startswith(("6", "5")):
        return "sh"
    return "sz"


def _quick_signal(market: str, code: str, target_date: str) -> dict | None:
    """对一只股票跑 K 线 + 技术指标 + 当日涨跌 → 综合信号。"""
    if market in ("sh", "sz"):
        kind = "astock"
        market_for_data = market
    elif market == "us":
        kind = "us"
        market_for_data = "us"
    else:
        return None

    target_dash = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"

    try:
        kline = fetch_kline(kind, code, market_for_data, target_date, lookback=120)
    except Exception:
        return None
    if kline is None or kline.empty or len(kline) < 30:
        return None

    kline = kline.sort_values("date").reset_index(drop=True)
    kline["date"] = kline["date"].astype(str)

    matched = kline[kline["date"] == target_dash]
    if matched.empty:
        # 目标日无数据（停牌/非交易日）→ 用最后一个交易日
        idx = len(kline) - 1
    else:
        idx = matched.index[0]

    if idx < 1:
        return None

    row = kline.iloc[idx]
    today_close = float(row["close"])
    prev_close = float(kline.iloc[idx - 1]["close"])
    pct = (today_close - prev_close) / prev_close * 100

    # 价格信号
    if pct >= 5: price_score = 1.0
    elif pct >= 2: price_score = 0.5
    elif pct >= 0: price_score = 0.2
    elif pct >= -2: price_score = -0.2
    elif pct >= -5: price_score = -0.5
    else: price_score = -1.0

    # 技术指标
    indi = compute_indicators(kline)
    actual_dash = str(row["date"])
    tech = analyze_tech(indi, actual_dash)

    tech_score = float(tech.get("score", 0))
    weighted_tech = tech_score * 0.7
    total = price_score + weighted_tech

    # verdict + 置信度
    if total >= 1.6:
        verdict, conf = "看多", "高"
    elif total >= 1.0:
        verdict, conf = "看多", "中"
    elif total >= 0.5:
        verdict, conf = "偏多", "中"
    elif total >= 0.2:
        verdict, conf = "偏多", "低"
    elif total <= -1.6:
        verdict, conf = "看空", "高"
    elif total <= -1.0:
        verdict, conf = "看空", "中"
    elif total <= -0.5:
        verdict, conf = "偏空", "中"
    else:
        verdict, conf = "震荡", "低"

    return {
        "market": market, "code": code,
        "actual_date": actual_dash,
        "close": today_close, "prev_close": prev_close, "pct_change": pct,
        "price_score": round(price_score, 2),
        "tech_score_raw": round(tech_score, 2),
        "tech_score_weighted": round(weighted_tech, 2),
        "tech_rules": tech.get("signals", []),
        "snapshot": tech.get("snapshot", {}),
        "total_score": round(total, 2),
        "verdict": verdict,
        "confidence": conf,
    }


# ---------------- 过滤 + 简约分析 ----------------

def _is_bullish_with_confidence(sig: dict) -> bool:
    """看涨且置信度 ≥ 中等。"""
    return (sig.get("verdict") in ("看多", "偏多")
            and sig.get("confidence") in ("中", "高"))


def _make_brief(rec: dict) -> str:
    """把信号摘要成一行简约分析。"""
    parts = [f"{rec['pct_change']:+.2f}%"]
    keep_keywords = ("多头排列", "金叉", "底背离", "上轨", "放量上涨",
                     "缩量", "突破", "超卖", "偏强")
    short = []
    for r in rec.get("tech_rules", []):
        head = r.split(" → ")[0].strip()
        if any(k in head for k in keep_keywords):
            short.append(head)
        if len(short) >= 2:
            break
    if short:
        parts.extend(short)
    parts.append(f"信号 {rec['total_score']:+.2f}")
    parts.append(f"[{rec['verdict']}/{rec['confidence']}]")
    return " ｜ ".join(parts)


# ---------------- 主入口 ----------------

def _scan_a_peer(p: dict, target_date: str) -> tuple[dict, dict | None] | None:
    """校验 + 拉单只 A 股信号。返回 (peer_meta, signal|None)；代码非法返回 None。"""
    code = str(p.get("code", "")).strip().zfill(6)
    if len(code) != 6 or code[0] in ("4", "8") or not code.isdigit():
        return None
    sig = _quick_signal(_market_for_a(code), code, target_date)
    return ({**p, "code": code}, sig)


def _scan_us_peer(p: dict, target_date: str) -> tuple[dict, dict | None] | None:
    ticker = str(p.get("ticker", "")).strip().upper()
    if not ticker or not all(c.isalpha() or c == "." for c in ticker):
        return None
    sig = _quick_signal("us", ticker, target_date)
    return ({**p, "ticker": ticker}, sig)


def scan_peers(target_name: str, target_code: str, target_market: str,
               target_date: str, industry_hint: str | None = None,
               top_n: int = 5, prefetched_rec: dict | None = None,
               max_workers: int = 8) -> dict:
    """返回结构：
    {
      "industry_zh": "...",
      "industry_en": "...",
      "a_stock": [{code, name, reason, brief, total_score, verdict, ...}, ...],
      "us_stock": [...],
      "stats": {"a_total": N, "a_kept": K, "us_total": N, "us_kept": K}
    }

    prefetched_rec：若上层已并发取到 Claude 的 peer 推荐，直接传入，省一次串行 LLM 往返。
    """
    if prefetched_rec is not None:
        rec = prefetched_rec
    else:
        print("  [Peer] 调可选 LLM 推荐同行业 peers...")
        rec = _suggest_peers(target_name, target_code, target_market, industry_hint)
    if not rec:
        return {"error": "peer 推荐失败"}

    a_in = rec.get("a_stock_peers") or []
    us_in = rec.get("us_stock_peers") or []
    print(f"  [Peer] 候选 A 股 {len(a_in)} 只 / 美股 {len(us_in)} 只 → 并发扫描")

    # 16 只 peer 的 K 线拉取彼此独立 → 线程池并发；保留输入顺序打印
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        a_scanned = list(pool.map(lambda p: _scan_a_peer(p, target_date), a_in))
        us_scanned = list(pool.map(lambda p: _scan_us_peer(p, target_date), us_in))

    a_results, us_results = [], []
    for item in a_scanned:
        if item is None:
            continue
        p, sig = item
        if not sig:
            print(f"    [-] {p.get('code','')} {p.get('name','')}: 无 K 线")
            continue
        print(f"    [{sig['verdict']}/{sig['confidence']}] {p['code']} {p.get('name','')}: "
              f"{sig['pct_change']:+.2f}% 信号 {sig['total_score']:+.2f}")
        if _is_bullish_with_confidence(sig):
            a_results.append({**p, **sig, "brief": _make_brief(sig)})

    for item in us_scanned:
        if item is None:
            continue
        p, sig = item
        if not sig:
            print(f"    [-] {p.get('ticker','')} {p.get('name','')}: 无 K 线")
            continue
        print(f"    [{sig['verdict']}/{sig['confidence']}] {p['ticker']} {p.get('name','')}: "
              f"{sig['pct_change']:+.2f}% 信号 {sig['total_score']:+.2f}")
        if _is_bullish_with_confidence(sig):
            us_results.append({**p, **sig, "brief": _make_brief(sig)})

    a_results.sort(key=lambda x: x["total_score"], reverse=True)
    us_results.sort(key=lambda x: x["total_score"], reverse=True)

    return {
        "industry_zh": rec.get("industry_zh"),
        "industry_en": rec.get("industry_en"),
        "stats": {
            "a_total": len(a_in), "a_kept": len(a_results),
            "us_total": len(us_in), "us_kept": len(us_results),
        },
        "a_stock": a_results[:top_n],
        "us_stock": us_results[:top_n],
    }
