"""信息储备层 — 事件面信号(P2)。

把加工后的事件卡聚合成一个信号贡献,作为偏情绪/基本面的二级信号,与期权流同级(内部 cap ±0.6),
避免淹没技术信号。

每条贡献 = sentiment × (materiality/3) × recency_decay
  - sentiment   : LLM 给的方向 [-1, +1]
  - materiality : LLM 给的影响力 0..3,归一到 [0,1] 作权重(噪音事件权重 0,自然不影响)
  - recency_decay = exp(-days_ago / HALFLIFE_DECAY):越近权重越高,90 天外趋近 0
总分 = Σ 贡献,clip 到 [-CAP, +CAP]。
"""
from __future__ import annotations

import math
from datetime import date

CAP = 0.6
DECAY_TAU = 30.0  # 天;exp(-days/30):30 天 0.37,60 天 0.14,90 天 0.05


def _days_between(event_date: str, target_dash: str) -> int:
    try:
        e = date.fromisoformat(event_date[:10])
        t = date.fromisoformat(target_dash[:10])
        return max((t - e).days, 0)
    except Exception:
        return 0


def compute_info_signal(cards: list[dict], target_dash: str) -> dict:
    """cards: query_events 返回的事件卡(应已是 event_date<=target 的窗口内卡)。"""
    contribs = []
    for c in cards:
        sent = c.get("sentiment")
        mat = c.get("materiality")
        if sent is None or mat is None:
            continue
        decay = math.exp(-_days_between(c.get("event_date", ""), target_dash) / DECAY_TAU)
        contrib = float(sent) * (float(mat) / 3.0) * decay
        if abs(contrib) < 1e-6:
            continue
        contribs.append((c, contrib))

    raw = sum(x for _, x in contribs)
    score = max(-CAP, min(CAP, raw))

    contribs.sort(key=lambda kv: abs(kv[1]), reverse=True)
    top = [
        {"date": c.get("event_date"), "type": c.get("type"),
         "title": (c.get("title") or "")[:40],
         "sentiment": c.get("sentiment"), "materiality": c.get("materiality"),
         "contrib": round(v, 3)}
        for c, v in contribs[:5]
    ]
    n_proc = sum(1 for c in cards if c.get("processed_at"))
    if not contribs:
        detail = f"窗口内 {len(cards)} 条事件,无显著方向(已加工 {n_proc})"
    else:
        drivers = "; ".join(f"{t['date']} {t['type']}({t['contrib']:+.2f})" for t in top[:3])
        detail = f"事件 {len(cards)} 条 raw {raw:+.2f}→{score:+.2f} | 主导: {drivers}"
    return {"score": round(score, 3), "raw": round(raw, 3), "n_events": len(cards),
            "n_processed": n_proc, "top": top, "detail": detail}
