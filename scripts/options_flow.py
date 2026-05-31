"""期权流分析 — 看市场用期权在押注什么方向。

覆盖范围（A 股个股、港股本身没有可交易期权，自动跳过）：
- A 股 ETF/指数期权：ak.option_daily_stats_sse / option_daily_stats_szse(date=YYYYMMDD)
    → 按标的给出 认购/认沽 成交量、未平仓量；支持历史日期，能对齐 target_date。
    隐含波动率走势取 QVIX 系列（index_option_*_qvix），同样有历史序列。
- 美股：yfinance.Ticker(code).option_chain(expiry) → 各到期日 call/put 的 volume / openInterest /
    impliedVolatility。⚠️ yfinance 只给"当前"期权链快照，无法回到历史 target_date，
    所以 target_date 与今天相差较远时会标注"快照为当前"。

核心指标：
- PCR (Put/Call Ratio)：认沽成交量 / 认购成交量。偏低=看涨持仓占优，偏高=认沽活跃（看跌/对冲）。
- 持仓 PCR：未平仓认沽 / 未平仓认购，反映存量仓位倾向。
- 隐含波动率(IV)及其 5 日趋势：IV 抬升=避险/对冲需求上升。

打分（内部 cap 在 ±0.6，作为偏情绪/资金面的二级信号计入总分）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Optional

import pandas as pd

from data_layer import _without_system_proxy, _apply_proxy_for_yfinance, _retry

logger = logging.getLogger(__name__)


# 期权 ETF 标的 → QVIX 日频隐含波动率函数（akshare）
_QVIX_FUNC = {
    "510050": "index_option_50etf_qvix",
    "510300": "index_option_300etf_qvix",
    "159919": "index_option_300etf_qvix",
    "510500": "index_option_500etf_qvix",
    "159922": "index_option_500etf_qvix",
    "588000": "index_option_kcb_qvix",
    "588080": "index_option_kcb_qvix",
    "159915": "index_option_cyb_qvix",
    "159901": "index_option_100etf_qvix",
}


# ---------------- A 股 ETF/指数期权 ----------------

def _fetch_cn_option_stats(target_date: str) -> Optional[pd.DataFrame]:
    """合并沪深两所当日期权标的统计，统一成：
    code/name/call_vol/put_vol/call_oi/put_oi。target_date: YYYYMMDD。"""
    import akshare as ak
    frames = []
    with _without_system_proxy():
        for fn, ex in (("option_daily_stats_sse", "上交所"),
                       ("option_daily_stats_szse", "深交所")):
            try:
                df = getattr(ak, fn)(date=target_date)
            except Exception as e:
                logger.warning(f"{fn}({target_date}) 失败: {e}")
                continue
            if df is None or df.empty:
                continue
            df = df.copy()
            df["_ex"] = ex
            frames.append(df)
    if not frames:
        return None

    rows = []
    for df in frames:
        for _, r in df.iterrows():
            code = str(r.get("合约标的代码", "")).strip()
            if not code:
                continue
            rows.append({
                "code": code,
                "name": str(r.get("合约标的名称", "")),
                "exchange": r.get("_ex"),
                "call_vol": _num(r.get("认购成交量")),
                "put_vol": _num(r.get("认沽成交量")),
                "call_oi": _num(r.get("未平仓认购合约数")),
                "put_oi": _num(r.get("未平仓认沽合约数")),
            })
    return pd.DataFrame(rows) if rows else None


def _fetch_iv_trend(code: str, target_dash: str) -> Optional[dict]:
    """QVIX 隐含波动率：取目标日 IV、近 5 日变化、近 1 年分位。"""
    fn = _QVIX_FUNC.get(code)
    if not fn:
        return None
    import akshare as ak
    f = getattr(ak, fn, None)
    if f is None:
        return None
    try:
        with _without_system_proxy():
            q = f()
    except Exception as e:
        logger.warning(f"{fn} 失败: {e}")
        return None
    if q is None or q.empty or "date" not in q.columns or "close" not in q.columns:
        return None
    q = q.copy()
    q["date"] = pd.to_datetime(q["date"]).dt.strftime("%Y-%m-%d")
    q = q[q["date"] <= target_dash].sort_values("date")
    if q.empty:
        return None
    iv = _num(q.iloc[-1]["close"])
    iv_5d_ago = _num(q.iloc[-6]["close"]) if len(q) >= 6 else None
    chg_5d = (iv - iv_5d_ago) if (iv is not None and iv_5d_ago is not None) else None
    # 近 1 年（~250 交易日）分位
    last_year = q.tail(250)["close"].astype(float)
    pct_rank = None
    if iv is not None and len(last_year) >= 30:
        pct_rank = float((last_year <= iv).mean())
    return {"iv": iv, "chg_5d": chg_5d, "percentile_1y": pct_rank, "as_of": q.iloc[-1]["date"]}


# ---------------- 美股期权链（yfinance） ----------------

def _us_cache_path(code: str) -> str:
    d = os.path.join(tempfile.gettempdir(), "stock_analyst_opt_cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"us_{code.upper()}.json")


def _fetch_us_option_flow(code: str, max_expiries: int = 2) -> Optional[dict]:
    """yfinance 期权链：聚合最近 max_expiries 个到期日的 call/put 成交量/未平仓/IV。

    带 1 小时文件缓存，避免重复触发 yfinance 限速。"""
    cache = _us_cache_path(code)
    try:
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3600:
            with open(cache, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass

    _apply_proxy_for_yfinance()
    import yfinance as yf
    tk = yf.Ticker(code.upper())
    try:
        expiries = _retry(lambda: list(tk.options), max_retries=3, label="yf.options")
    except Exception as e:
        logger.warning(f"yfinance options 列表失败 {code}: {e}")
        return None
    if not expiries:
        return None

    call_vol = put_vol = call_oi = put_oi = 0.0
    ivs: list[float] = []
    used: list[str] = []
    for exp in expiries[:max_expiries]:
        try:
            chain = _retry(lambda e=exp: tk.option_chain(e), max_retries=2,
                           label=f"yf.option_chain[{exp}]")
        except Exception as e:
            logger.warning(f"option_chain {code} {exp} 失败: {e}")
            continue
        for df, is_call in ((chain.calls, True), (chain.puts, False)):
            if df is None or df.empty:
                continue
            v = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0).sum()
            oi = pd.to_numeric(df.get("openInterest"), errors="coerce").fillna(0).sum()
            iv_col = pd.to_numeric(df.get("impliedVolatility"), errors="coerce").dropna()
            iv_col = iv_col[(iv_col > 0) & (iv_col < 5)]
            if not iv_col.empty:
                ivs.extend(iv_col.tolist())
            if is_call:
                call_vol += float(v); call_oi += float(oi)
            else:
                put_vol += float(v); put_oi += float(oi)
        used.append(exp)

    if not used or (call_vol == 0 and put_vol == 0):
        return None
    iv_avg = float(pd.Series(ivs).median()) if ivs else None
    result = {
        "call_vol": call_vol, "put_vol": put_vol,
        "call_oi": call_oi, "put_oi": put_oi,
        "iv_avg": iv_avg, "expiries_used": used,
    }
    try:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
    except Exception:
        pass
    return result


# ---------------- 打分 ----------------

def _safe_ratio(put: float | None, call: float | None) -> Optional[float]:
    if put is None or call is None or call <= 0:
        return None
    return put / call


def _score_flow(pcr_vol: float | None, pcr_oi: float | None,
                iv_trend: dict | None) -> tuple[float, str]:
    """PCR + 持仓 PCR + IV 趋势 → 内部得分（cap ±0.6）与说明。

    认沽/认购偏低=看涨占优(+)，偏高=认沽活跃/对冲(-)；极端值逆向减弱。"""
    if pcr_vol is None:
        return 0.0, "PCR 不可用"
    if pcr_vol < 0.6:
        base, tag = 0.35, "认沽/认购成交比偏低，看涨持仓占优"
    elif pcr_vol < 0.85:
        base, tag = 0.2, "成交略偏看涨"
    elif pcr_vol <= 1.15:
        base, tag = 0.0, "多空成交均衡"
    elif pcr_vol <= 1.6:
        base, tag = -0.2, "认沽活跃，偏谨慎/对冲需求上升"
    else:
        base, tag = -0.1, "认沽极端放大，或临近恐慌（逆向减弱）"

    # 持仓 PCR 同向加强
    if pcr_oi is not None:
        if pcr_oi < 0.85 and base >= 0:
            base += 0.15
        elif pcr_oi > 1.2 and base <= 0:
            base -= 0.15

    # IV 趋势微调
    iv_note = ""
    if iv_trend and iv_trend.get("chg_5d") is not None and iv_trend.get("iv"):
        chg = iv_trend["chg_5d"]
        iv = iv_trend["iv"]
        rel = chg / iv if iv else 0
        if rel > 0.15:
            base -= 0.1
            iv_note = f"，IV 近 5 日 +{chg:.1f} 抬升（避险升温）"
        elif rel < -0.15:
            base += 0.1
            iv_note = f"，IV 近 5 日 {chg:.1f} 回落（情绪转松）"

    base = max(-0.6, min(0.6, base))
    pcr_str = f"PCR(量) {pcr_vol:.2f}"
    if pcr_oi is not None:
        pcr_str += f" / PCR(仓) {pcr_oi:.2f}"
    return round(base, 2), f"{pcr_str} — {tag}{iv_note}"


# ---------------- 主入口 ----------------

def analyze_options_flow(kind: str, code: str, market: str,
                         target_dash: str) -> dict:
    """返回期权流分析 dict。

    {available, market_type, score, detail, ...metrics}
    无可交易期权（A 股个股 / 港股）时 available=False，score=0。"""
    if kind == "hk":
        return {"available": False, "score": 0.0,
                "detail": "港股个股期权 akshare/yfinance 覆盖不足，跳过"}

    # A 股个股 / ETF / 指数 → 查当日两所期权标的统计
    if kind in ("astock", "etf"):
        target_yyyymmdd = target_dash.replace("-", "")
        stats = _fetch_cn_option_stats(target_yyyymmdd)
        if stats is None:
            return {"available": False, "score": 0.0,
                    "detail": f"无法获取 {target_dash} 期权标的统计"}
        sub = stats[stats["code"] == code]
        if sub.empty:
            return {"available": False, "score": 0.0,
                    "detail": f"{code} 非期权标的（A 股个股无个股期权），跳过"}
        r = sub.iloc[0]
        pcr_vol = _safe_ratio(r["put_vol"], r["call_vol"])
        pcr_oi = _safe_ratio(r["put_oi"], r["call_oi"])
        iv_trend = _fetch_iv_trend(code, target_dash)
        score, detail = _score_flow(pcr_vol, pcr_oi, iv_trend)
        return {
            "available": True, "market_type": "cn_etf_index_option",
            "underlying": code, "underlying_name": r["name"],
            "exchange": r["exchange"],
            "call_vol": r["call_vol"], "put_vol": r["put_vol"],
            "call_oi": r["call_oi"], "put_oi": r["put_oi"],
            "pcr_volume": _round(pcr_vol), "pcr_oi": _round(pcr_oi),
            "iv": (iv_trend or {}).get("iv"),
            "iv_chg_5d": (iv_trend or {}).get("chg_5d"),
            "iv_percentile_1y": (iv_trend or {}).get("percentile_1y"),
            "source": "沪深交所期权日报" + ("+QVIX" if iv_trend else ""),
            "score": score, "detail": detail,
        }

    # 美股
    if kind == "us":
        flow = _fetch_us_option_flow(code)
        if flow is None:
            return {"available": False, "score": 0.0,
                    "detail": f"{code} 无期权链数据（yfinance 限速或无期权）"}
        pcr_vol = _safe_ratio(flow["put_vol"], flow["call_vol"])
        pcr_oi = _safe_ratio(flow["put_oi"], flow["call_oi"])
        iv_avg = flow.get("iv_avg")
        score, detail = _score_flow(pcr_vol, pcr_oi, None)
        # 快照时效说明：yfinance 只给当前期权链
        stale = ""
        try:
            days = (datetime.now() - datetime.strptime(target_dash, "%Y-%m-%d")).days
            if abs(days) > 5:
                stale = "（注：yfinance 期权链为当前快照，非目标日）"
        except Exception:
            pass
        if iv_avg is not None:
            detail += f"，平均 IV {iv_avg*100:.1f}%"
        return {
            "available": True, "market_type": "us_option",
            "underlying": code.upper(),
            "call_vol": flow["call_vol"], "put_vol": flow["put_vol"],
            "call_oi": flow["call_oi"], "put_oi": flow["put_oi"],
            "pcr_volume": _round(pcr_vol), "pcr_oi": _round(pcr_oi),
            "iv": _round(iv_avg * 100) if iv_avg is not None else None,
            "expiries_used": flow.get("expiries_used"),
            "source": "yfinance option_chain" + stale,
            "score": score, "detail": detail + stale,
        }

    return {"available": False, "score": 0.0, "detail": "未知市场类型"}


# ---------------- 工具 ----------------

def _num(v) -> Optional[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _round(v, n: int = 2):
    return round(v, n) if isinstance(v, (int, float)) else v
