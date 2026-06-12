"""多因子打分卡 — 风格 / 技术 / 基本面 / 板块相对强度，带因子有效性检验。

四大类因子：
- style:              价值（PE_TTM 历史分位）/ 估值（PB 历史分位）/ 盈利能力（PEG）
- technical:          短期动量(5d)/ 中期动量(20d)/ 波动率分位 / 量比 / 布林位置
- fundamental:        ROE / 销售毛利率 / 资产负债率 / 净利润同比
- relative_strength:  个股 vs 行业(peers 平均) / 个股 vs 上证指数

每个因子条目：
{
    "name": str,                    # 中文名
    "value": float|str,             # 原始值
    "raw_score": float,             # 原始打分 -1..+1
    "score": float,                 # 经有效性权重调整后的最终分
    "remark": str,                  # 文字解读
    "validation_key": str|None,     # 对应 factor_validation 的 key（None=静态因子）
    "validation": dict|None,        # IC/IR/grade（仅时序因子有）
    "weight_multiplier": float,     # A/B=1.0, C=0.5, D=0.0, 静态=1.0
}

类别分 = items 平均分（用最终 score）；总因子分 = 各类别按权重合成。

A 股全功能；港股 / ETF / 美股只跑能跑的部分。
有效性检验在 A 股全功能模式下默认开启，过滤后仅保留通过 |IC|≥0.06 + |IR|≥0.5 的因子（C 级半权重，D 级剔除）。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Optional, Any

import akshare as ak
import numpy as np
import pandas as pd

from factor_validation import (
    validate_technical_factors, validate_style_factors,
    validate_relative_strength, summarize_validation,
)

logger = logging.getLogger(__name__)


# ---------------- 评级 → 权重映射 ----------------

GRADE_MULTIPLIER = {
    "A": 1.0,    # 精品因子
    "B": 1.0,    # 通过
    "C": 0.5,    # 弱有效，半权重
    "D": 0.0,    # 不达标，剔除
    "?": 1.0,    # 样本不足，按"未检验"对待，原权重保留
}


def _attach_validation(item: dict, vkey: str | None,
                       validation_results: dict) -> dict:
    """给因子条目附加 IC/IR/grade，并按 grade 调整 score。"""
    item["validation_key"] = vkey
    item["raw_score"] = item.get("score", 0.0)

    if vkey is None or vkey not in validation_results:
        # 静态因子（财务/PEG/RS 当日），不做检验，原分数保留
        item["validation"] = None
        item["weight_multiplier"] = 1.0
        return item

    v = validation_results[vkey]
    item["validation"] = {
        "IC": v.get("IC"),
        "IR": v.get("IR"),
        "grade": v.get("grade"),
        "samples": v.get("samples"),
        "passes": v.get("passes"),
        "direction_correct": v.get("direction_correct"),
    }
    grade = v.get("grade", "?")
    mult = GRADE_MULTIPLIER.get(grade, 1.0)
    item["weight_multiplier"] = mult
    item["score"] = item["raw_score"] * mult
    return item


# ---------------- 代理工具 ----------------

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
               "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")


@contextmanager
def _no_proxy():
    saved = {k: os.environ[k] for k in _PROXY_KEYS if k in os.environ}
    for k in _PROXY_KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k in _PROXY_KEYS:
            os.environ.pop(k, None)
        os.environ.update(saved)


# ---------------- 工具：分位映射 → 评分 ----------------

def _pct_to_score_low_better(p: float) -> float:
    """估值分位（PE/PB）：分位越低（便宜）→ +1，越高（贵）→ -1。"""
    if p is None or np.isnan(p):
        return 0.0
    if p <= 0.20:  return +1.0
    if p <= 0.35:  return +0.5
    if p <= 0.65:  return 0.0
    if p <= 0.80:  return -0.5
    return -1.0


def _pct_to_score_high_better(p: float) -> float:
    """高分位更好（如 ROE/动量分位）。"""
    if p is None or np.isnan(p):
        return 0.0
    if p >= 0.80:  return +1.0
    if p >= 0.65:  return +0.5
    if p >= 0.35:  return 0.0
    if p >= 0.20:  return -0.5
    return -1.0


def _historical_percentile(series: pd.Series, current: float) -> Optional[float]:
    """返回 current 在 series 历史里的分位（0 = 最低，1 = 最高）。"""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 30 or current is None or np.isnan(current):
        return None
    return float((s <= current).mean())


# ---------------- 风格因子（A 股估值分位）----------------

def _style_factors_astock(code: str) -> dict:
    """A 股风格因子旧外部源已禁用。"""
    return {"error": "A 股估值外部源已禁用"}


def _style_factors_astock_legacy(code: str) -> dict:
    try:
        with _no_proxy():
            df = ak.stock_value_em(symbol=code)
    except Exception as e:
        return {"error": f"stock_value_em 失败: {type(e).__name__}"}

    if df is None or df.empty:
        return {"error": "stock_value_em 无数据"}

    df = df.copy()
    df["数据日期"] = pd.to_datetime(df["数据日期"], errors="coerce")
    df = df.sort_values("数据日期")
    # 只取近 3 年
    cutoff = df["数据日期"].max() - pd.Timedelta(days=365 * 3)
    df3y = df[df["数据日期"] >= cutoff]

    last = df.iloc[-1]
    items: list[dict] = []

    # 价值：PE_TTM 历史分位
    pe = float(last.get("PE(TTM)", np.nan))
    pe_pct = _historical_percentile(df3y["PE(TTM)"], pe)
    items.append({
        "name": "PE(TTM) 3 年分位",
        "value": f"{pe:.2f}x" if not np.isnan(pe) else "N/A",
        "score": _pct_to_score_low_better(pe_pct or np.nan),
        "remark": (f"近 3 年 {pe_pct*100:.0f}% 分位"
                   + ("（便宜）" if (pe_pct or 1) <= 0.35
                      else "（贵）" if (pe_pct or 0) >= 0.65
                      else "（中性）") if pe_pct is not None else "数据不足"),
        "validation_key": "style_pe_pct_3y",
    })

    # 估值：PB 分位
    pb = float(last.get("市净率", np.nan))
    pb_pct = _historical_percentile(df3y["市净率"], pb)
    items.append({
        "name": "PB 3 年分位",
        "value": f"{pb:.2f}x" if not np.isnan(pb) else "N/A",
        "score": _pct_to_score_low_better(pb_pct or np.nan),
        "remark": (f"近 3 年 {pb_pct*100:.0f}% 分位"
                   + ("（便宜）" if (pb_pct or 1) <= 0.35
                      else "（贵）" if (pb_pct or 0) >= 0.65
                      else "（中性）") if pb_pct is not None else "数据不足"),
        "validation_key": "style_pb_pct_3y",
    })

    # PEG（结合成长的估值，<1 偏多，>2 偏空）— 静态因子
    peg = float(last.get("PEG值", np.nan))
    peg_score = 0.0
    peg_remark = "无 PEG"
    if not np.isnan(peg) and peg > 0:
        if peg <= 0.5:    peg_score, peg_remark = +1.0, "PEG ≤ 0.5 极低估"
        elif peg <= 1.0:  peg_score, peg_remark = +0.5, "PEG ≤ 1 合理偏低"
        elif peg <= 1.5:  peg_score, peg_remark = 0.0, "PEG 1-1.5 合理"
        elif peg <= 2.5:  peg_score, peg_remark = -0.5, "PEG 1.5-2.5 偏高"
        else:             peg_score, peg_remark = -1.0, "PEG > 2.5 高估"
    elif not np.isnan(peg) and peg < 0:
        peg_remark = "PEG < 0（盈利下滑或亏损）"
        peg_score = -0.3
    items.append({
        "name": "PEG",
        "value": f"{peg:.2f}" if not np.isnan(peg) else "N/A",
        "score": peg_score,
        "remark": peg_remark,
        "validation_key": None,  # 静态因子
    })

    return {
        "items": items,
        "snapshot_date": str(last["数据日期"].date()),
        "value_em_df": df,  # 留给验证器复用
    }


# ---------------- 技术因子（所有市场都能跑）----------------

def _technical_factors(kline: pd.DataFrame, target_dash: str) -> dict:
    """从 K 线计算技术因子。kline 需含 date / close / volume 列且按日期升序。"""
    df = kline.copy()
    df["date"] = df["date"].astype(str)
    if target_dash in df["date"].values:
        idx = df.index[df["date"] == target_dash][0]
    else:
        idx = len(df) - 1  # fallback 到最后一行

    if idx < 20:
        return {"error": "K 线数据不足 20 日"}

    sub = df.iloc[: idx + 1].copy()
    close = pd.to_numeric(sub["close"], errors="coerce")
    vol = pd.to_numeric(sub["volume"], errors="coerce")

    items: list[dict] = []

    # 短期动量（5 日收益率）
    if len(close) >= 6:
        ret_5 = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)
        if ret_5 >= 8:    s5 = +1.0
        elif ret_5 >= 3:  s5 = +0.5
        elif ret_5 <= -8: s5 = -1.0
        elif ret_5 <= -3: s5 = -0.5
        else:             s5 = 0.0
        items.append({
            "name": "5 日动量",
            "value": f"{ret_5:+.2f}%",
            "score": s5,
            "remark": "强势上行" if ret_5 >= 5 else ("回调" if ret_5 <= -5 else "震荡"),
            "validation_key": "tech_momentum_5d",
        })

    # 中期动量（20 日）
    if len(close) >= 21:
        ret_20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100)
        if ret_20 >= 20:   s20 = +1.0
        elif ret_20 >= 8:  s20 = +0.5
        elif ret_20 <= -20: s20 = -1.0
        elif ret_20 <= -8: s20 = -0.5
        else:              s20 = 0.0
        items.append({
            "name": "20 日动量",
            "value": f"{ret_20:+.2f}%",
            "score": s20,
            "remark": "中期趋势向上" if ret_20 >= 10 else
                      ("中期趋势向下" if ret_20 <= -10 else "中期横盘"),
            "validation_key": "tech_momentum_20d",
        })

    # 波动率（20 日年化波动 vs 自身近 1 年分位）
    if len(close) >= 21:
        rets = close.pct_change().dropna()
        vol20 = float(rets.tail(20).std() * np.sqrt(252) * 100)
        # 历史 vol 分位（用过去所有 20 日滚动 vol）
        hist_vol = rets.rolling(20).std().dropna() * np.sqrt(252) * 100
        vp = _historical_percentile(hist_vol, vol20) if len(hist_vol) >= 30 else None
        # 高波动倾向 -0.3（短线风险），但这是个偏弱的信号
        if vp is None:
            vs = 0.0; vrem = "波动率历史样本不足"
        elif vp >= 0.85: vs, vrem = -0.5, f"年化波动 {vol20:.1f}%（{vp*100:.0f}% 分位高位，风险大）"
        elif vp >= 0.65: vs, vrem = -0.2, f"年化波动 {vol20:.1f}%（偏高）"
        elif vp <= 0.20: vs, vrem = +0.3, f"年化波动 {vol20:.1f}%（{vp*100:.0f}% 分位低位，企稳）"
        else:            vs, vrem = 0.0, f"年化波动 {vol20:.1f}%（中性）"
        items.append({
            "name": "20 日波动率",
            "value": f"{vol20:.1f}%",
            "score": vs,
            "remark": vrem,
            "validation_key": "tech_volatility",
        })

    # 量比（5 日量 / 20 日量）
    if len(vol) >= 21:
        ma5_v = float(vol.tail(5).mean())
        ma20_v = float(vol.tail(20).mean())
        if ma20_v > 0:
            vr = ma5_v / ma20_v
            # 看价格方向决定放量分数
            ret_5 = (close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0
            if vr >= 1.5 and ret_5 > 0:    vrs = +0.7; rmk = f"量比 {vr:.2f} 放量上涨"
            elif vr >= 1.5 and ret_5 < 0:  vrs = -0.7; rmk = f"量比 {vr:.2f} 放量下跌"
            elif vr >= 1.2:                vrs = +0.3 if ret_5 >= 0 else -0.3; rmk = f"量比 {vr:.2f} 温和放量"
            elif vr <= 0.7:                vrs = -0.2; rmk = f"量比 {vr:.2f} 缩量"
            else:                          vrs = 0.0; rmk = f"量比 {vr:.2f} 平稳"
            items.append({
                "name": "量比(5/20日均量)",
                "value": f"{vr:.2f}",
                "score": vrs,
                "remark": rmk,
                "validation_key": "tech_vol_ratio",
            })

    # 布林位置 BB%
    if len(close) >= 20:
        mid = close.rolling(20).mean().iloc[-1]
        std = close.rolling(20).std().iloc[-1]
        if std > 0:
            up = mid + 2 * std
            dn = mid - 2 * std
            bbp = float((close.iloc[-1] - dn) / (up - dn))
            if bbp >= 1:    bs = -0.3; brem = f"BB%={bbp:.2f} 突破上轨（超买）"
            elif bbp >= 0.8: bs = +0.5; brem = f"BB%={bbp:.2f} 接近上轨（强势）"
            elif bbp <= 0:  bs = +0.5; brem = f"BB%={bbp:.2f} 跌破下轨（反弹）"
            elif bbp <= 0.2: bs = -0.5; brem = f"BB%={bbp:.2f} 接近下轨（弱势）"
            else:           bs = 0.0; brem = f"BB%={bbp:.2f} 布林通道中段"
            items.append({
                "name": "布林位置",
                "value": f"{bbp:.2f}",
                "score": bs,
                "remark": brem,
                "validation_key": "tech_boll_pct",
            })

    if not items:
        return {"error": "技术因子计算失败"}

    return {"items": items}


# ---------------- 基本面因子（A 股）----------------

def _fundamental_factors_astock(code: str) -> dict:
    """A 股基本面因子旧外部源已禁用。"""
    return {"error": "A 股财务外部源已禁用"}


def _fundamental_factors_astock_legacy(code: str) -> dict:
    try:
        # 默认拿最近两年
        from datetime import datetime
        year = datetime.now().year - 1
        with _no_proxy():
            df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(year))
    except Exception as e:
        return {"error": f"financial_analysis_indicator 失败: {type(e).__name__}"}

    if df is None or df.empty:
        return {"error": "财务指标无数据"}

    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    df = df.sort_values("日期")
    last = df.iloc[-1]
    period = str(last["日期"].date())
    items: list[dict] = []

    # ROE（净资产收益率 %）
    roe = float(pd.to_numeric(last.get("净资产收益率(%)"), errors="coerce"))
    if not np.isnan(roe):
        if roe >= 15:    rs = +1.0; rrem = "ROE 优秀(≥15%)"
        elif roe >= 8:   rs = +0.5; rrem = "ROE 良好"
        elif roe >= 3:   rs = 0.0; rrem = "ROE 一般"
        elif roe >= 0:   rs = -0.5; rrem = "ROE 偏低"
        else:            rs = -1.0; rrem = "ROE 为负"
        items.append({
            "name": "ROE(净资产收益率)",
            "value": f"{roe:.2f}%",
            "score": rs, "remark": rrem,
            "validation_key": None,
        })

    # 销售毛利率
    gm = float(pd.to_numeric(last.get("销售毛利率"), errors="coerce"))
    if not np.isnan(gm):
        if gm >= 40:   gs = +1.0; grem = "毛利率优秀"
        elif gm >= 25: gs = +0.5; grem = "毛利率良好"
        elif gm >= 15: gs = 0.0; grem = "毛利率一般"
        elif gm >= 5:  gs = -0.5; grem = "毛利率偏低"
        else:          gs = -1.0; grem = "毛利率极低/为负"
        items.append({
            "name": "销售毛利率",
            "value": f"{gm:.2f}%",
            "score": gs, "remark": grem,
            "validation_key": None,
        })

    # 资产负债率（越低越稳健）
    dr = float(pd.to_numeric(last.get("资产负债率(%)"), errors="coerce"))
    if not np.isnan(dr):
        if dr <= 30:    ds = +0.5; drem = "杠杆低、财务稳健"
        elif dr <= 50:  ds = +0.2; drem = "杠杆适中"
        elif dr <= 70:  ds = -0.3; drem = "杠杆偏高"
        else:           ds = -0.8; drem = "杠杆很高，风险大"
        items.append({
            "name": "资产负债率",
            "value": f"{dr:.2f}%",
            "score": ds, "remark": drem,
            "validation_key": None,
        })

    # 净利润同比
    pg = float(pd.to_numeric(last.get("净利润增长率(%)"), errors="coerce"))
    if not np.isnan(pg):
        if pg >= 50:    pgs = +1.0; pgrm = f"净利同比 {pg:+.0f}% 高速增长"
        elif pg >= 20:  pgs = +0.7; pgrm = f"净利同比 {pg:+.0f}% 较快增长"
        elif pg >= 5:   pgs = +0.3; pgrm = f"净利同比 {pg:+.0f}% 稳定增长"
        elif pg >= -10: pgs = 0.0; pgrm = f"净利同比 {pg:+.0f}% 持平"
        elif pg >= -30: pgs = -0.5; pgrm = f"净利同比 {pg:+.0f}% 下滑"
        else:           pgs = -1.0; pgrm = f"净利同比 {pg:+.0f}% 大幅下滑"
        items.append({
            "name": "净利润同比",
            "value": f"{pg:+.2f}%",
            "score": pgs, "remark": pgrm,
            "validation_key": None,
        })

    # 营收同比
    rg = float(pd.to_numeric(last.get("主营业务收入增长率(%)"), errors="coerce"))
    if not np.isnan(rg):
        if rg >= 30:    rgs = +1.0; rgrm = f"营收同比 {rg:+.0f}% 高增"
        elif rg >= 10:  rgs = +0.5; rgrm = f"营收同比 {rg:+.0f}% 增长"
        elif rg >= 0:   rgs = +0.1; rgrm = f"营收同比 {rg:+.0f}% 微增"
        elif rg >= -10: rgs = -0.3; rgrm = f"营收同比 {rg:+.0f}% 微降"
        else:           rgs = -0.8; rgrm = f"营收同比 {rg:+.0f}% 下滑"
        items.append({
            "name": "营收同比",
            "value": f"{rg:+.2f}%",
            "score": rgs, "remark": rgrm,
            "validation_key": None,
        })

    if not items:
        return {"error": "无可用财务指标"}

    return {"items": items, "report_period": period}


# ---------------- 板块相对强度因子 ----------------

def _relative_strength_factors(
    kline: pd.DataFrame,
    target_dash: str,
    peers: Optional[dict] = None,
    market: str = "astock",
) -> dict:
    """相对强度旧外部源已禁用。"""
    return {"error": "相对强度外部源已禁用"}


def _relative_strength_factors_legacy(
    kline: pd.DataFrame,
    target_dash: str,
    peers: Optional[dict] = None,
    market: str = "astock",
) -> dict:
    items: list[dict] = []

    # 个股 20 日收益
    df = kline.copy()
    df["date"] = df["date"].astype(str)
    if len(df) < 21:
        return {"error": "K 线不足 20 日"}
    close = pd.to_numeric(df["close"], errors="coerce")
    stock_ret_20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100)

    # 1) 个股 vs 同行业 peers 平均涨跌（用 peers 当日数据）
    if peers and isinstance(peers, dict):
        peer_pcts: list[float] = []
        for r in (peers.get("a_stock") or []) + (peers.get("us_stock") or []):
            p = r.get("pct_change")
            if isinstance(p, (int, float)) and not np.isnan(p):
                peer_pcts.append(float(p))
        if len(peer_pcts) >= 3:
            peer_avg = float(np.mean(peer_pcts))
            stock_pct_today = None
            # 取个股当日涨跌
            if len(close) >= 2:
                stock_pct_today = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
            if stock_pct_today is not None:
                rs = stock_pct_today - peer_avg
                if rs >= 3:    rss = +1.0; rsrm = f"个股 {stock_pct_today:+.2f}% 显著强于行业均值 {peer_avg:+.2f}%"
                elif rs >= 1:  rss = +0.5; rsrm = f"个股 {stock_pct_today:+.2f}% 略强于行业 {peer_avg:+.2f}%"
                elif rs >= -1: rss = 0.0; rsrm = f"与行业（{peer_avg:+.2f}%）同步"
                elif rs >= -3: rss = -0.5; rsrm = f"个股 {stock_pct_today:+.2f}% 落后行业 {peer_avg:+.2f}%"
                else:          rss = -1.0; rsrm = f"个股 {stock_pct_today:+.2f}% 显著弱于行业 {peer_avg:+.2f}%"
                items.append({
                    "name": "vs 行业当日 RS",
                    "value": f"{rs:+.2f}pct",
                    "score": rss,
                    "remark": rsrm,
                    "validation_key": None,  # 当日单点，无法做时序 IC
                })

    # 2) 个股 vs 大盘指数（A 股用上证综指）
    if market == "astock":
        try:
            with _no_proxy():
                idx_df = ak.stock_zh_index_daily(symbol="sh000001")
            if idx_df is not None and not idx_df.empty:
                idx_df = idx_df.copy()
                idx_df["date"] = pd.to_datetime(idx_df["date"]).dt.strftime("%Y-%m-%d")
                idx_close = pd.to_numeric(idx_df["close"], errors="coerce")
                if len(idx_close) >= 21:
                    idx_ret_20 = float((idx_close.iloc[-1] / idx_close.iloc[-21] - 1) * 100)
                    rs20 = stock_ret_20 - idx_ret_20
                    if rs20 >= 15:    is_ = +1.0; irem = f"近 20 日跑赢大盘 {rs20:+.1f}pct（强势龙头）"
                    elif rs20 >= 5:   is_ = +0.5; irem = f"近 20 日跑赢大盘 {rs20:+.1f}pct"
                    elif rs20 >= -5:  is_ = 0.0; irem = f"与大盘同步（{rs20:+.1f}pct）"
                    elif rs20 >= -15: is_ = -0.5; irem = f"近 20 日跑输大盘 {rs20:+.1f}pct"
                    else:             is_ = -1.0; irem = f"近 20 日大幅跑输 {rs20:+.1f}pct（弱势）"
                    items.append({
                        "name": "vs 上证 20 日 RS",
                        "value": f"{rs20:+.2f}pct",
                        "score": is_,
                        "remark": irem,
                        "validation_key": "rs_vs_index_20d",
                        "_index_kline": idx_df,  # 留给验证器复用
                    })
        except Exception as e:
            logger.debug(f"index daily failed: {e}")

    if not items:
        return {"error": "板块/大盘相对强度数据不足"}

    return {"items": items}


# ---------------- 顶层入口 ----------------

def compute_factors(
    kline: pd.DataFrame,
    code: str,
    market: str,
    target_dash: str,
    peers: Optional[dict] = None,
    validate: bool = True,
) -> dict:
    """计算 4 类因子并跑因子有效性检验。

    market: 'astock'|'hk'|'etf'|'us'
    validate: 是否做 IC/IR 检验（默认 True；False 时所有时序因子按 grade='?' 全权计入）

    返回结构：
    {
        "categories": {
            "technical":         {"items": [...], "category_score": x},
            "style":             {...},
            "fundamental":       {...},
            "relative_strength": {...},
        },
        "validation": {
            "results": {factor_key: {IC, IR, grade, ...}},
            "summary": {total, passed, grades, mean_abs_IC, mean_abs_IR},
            "thresholds": {IC: 0.06, IR: 0.5},
        },
        "weighted_score": float,
        "category_scores": {...},
    }
    """
    out: dict[str, Any] = {"categories": {}}

    # 1) 计算各类因子原始打分。外部基本面 / 估值 / 指数源已禁用，仅保留 K 线派生技术因子。
    out["categories"]["technical"] = _technical_factors(kline, target_dash)

    # 2) 因子有效性检验
    validation_results: dict = {}
    if validate:
        try:
            validation_results.update(validate_technical_factors(kline))
        except Exception as e:
            logger.warning(f"技术因子验证失败: {e}")

        # 风格因子：从 _style_factors_astock 已经拉过的 value_em_df 复用
        style_data = out["categories"].get("style") or {}
        if isinstance(style_data, dict) and "value_em_df" in style_data:
            try:
                validation_results.update(
                    validate_style_factors(style_data["value_em_df"])
                )
            except Exception as e:
                logger.warning(f"风格因子验证失败: {e}")

        # 相对强度：从 _relative_strength_factors 已经拉过的 idx_df 复用
        rs_data = out["categories"].get("relative_strength") or {}
        if isinstance(rs_data, dict):
            for it in rs_data.get("items", []):
                if "_index_kline" in it:
                    try:
                        validation_results.update(
                            validate_relative_strength(kline, it["_index_kline"])
                        )
                    except Exception as e:
                        logger.warning(f"相对强度验证失败: {e}")
                    break

    # 3) 把验证结果注入每个 item，并按评级调权重
    for cat_data in out["categories"].values():
        if not isinstance(cat_data, dict) or "items" not in cat_data:
            continue
        for it in cat_data["items"]:
            vkey = it.get("validation_key")
            _attach_validation(it, vkey, validation_results)
        # 重新计算 category_score（用调整后的 score）
        # D 级因子 score=0 还是会平摊掉 category 分母。这里把 D 级因子从分母也剔除
        kept = [x for x in cat_data["items"] if x.get("weight_multiplier", 1.0) > 0]
        if kept:
            cat_data["category_score"] = float(np.mean([x["score"] for x in kept]))
            cat_data["n_items_kept"] = len(kept)
            cat_data["n_items_total"] = len(cat_data["items"])
        else:
            cat_data["category_score"] = 0.0
            cat_data["n_items_kept"] = 0
            cat_data["n_items_total"] = len(cat_data["items"])

        # 清理临时字段（不写进 JSON）
        cat_data.pop("value_em_df", None)
        for it in cat_data["items"]:
            it.pop("_index_kline", None)

    # 4) 汇总验证报告
    out["validation"] = {
        "results": validation_results,
        "summary": summarize_validation(validation_results),
        "thresholds": {
            "IC": 0.06, "IR": 0.5,
            "grade_A": "IC≥0.10 + IR≥1.0",
            "grade_B": "IC≥0.06 + IR≥0.5（通过）",
            "grade_C": "仅一项通过（半权重）",
            "grade_D": "都不达标（剔除）",
        },
    }

    # 5) 类别加权
    cat_scores: dict[str, float] = {}
    for k, v in out["categories"].items():
        if isinstance(v, dict) and "category_score" in v:
            cat_scores[k] = float(v["category_score"])

    weights = {
        "technical":          0.35,
        "style":              0.20,
        "fundamental":        0.25,
        "relative_strength":  0.20,
    }
    total_w = sum(weights[k] for k in cat_scores) or 1.0
    weighted = (sum(cat_scores[k] * weights[k] for k in cat_scores) / total_w
                if cat_scores else 0.0)

    out["category_scores"] = cat_scores
    out["weighted_score"] = float(weighted)
    out["weights_used"] = {k: weights[k] / total_w for k in cat_scores}
    return out
