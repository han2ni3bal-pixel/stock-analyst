"""因子有效性检验 — 单股时序 IC + 滚动 IR。

单股票场景做横截面 IC 没有意义（需要全市场截面数据），改用时序方法：
- ts-IC: 因子原始序列与 K 日远期收益的 Spearman 秩相关
- 滚动 IR: 60 日滚动 IC 的 mean / std（衡量因子稳定性）
- 方向性: 因子高分位平均后续收益是否大于低分位

阈值（可调）：
- |IC| ≥ 0.06 通过 IC 检验
- |IR| ≥ 0.5  通过 IR 检验
- 两个都过 = B 级以上；只过一个 = C 级；都不过 = D 级（剔除）
- IC ≥ 0.10 且 IR ≥ 1.0 = A 级（精品）

注：因子可能"反向有效"——比如 20 日波动率 IC 为负也算有效，
表示"低波动 → 高未来收益"。我们看 |IC|，符号决定打分方向。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_FWD_RETURN_DAYS = 5
DEFAULT_ROLLING_WINDOW = 60
DEFAULT_MIN_SAMPLES = 100

# 阈值
IC_THRESHOLD = 0.06
IR_THRESHOLD = 0.5
IC_GREAT = 0.10
IR_GREAT = 1.0


def _grade(abs_ic: float, abs_ir: float) -> str:
    """A: 优秀 / B: 通过 / C: 弱 / D: 不达标。"""
    if abs_ic is None or abs_ir is None:
        return "?"
    if abs_ic >= IC_GREAT and abs_ir >= IR_GREAT:
        return "A"
    if abs_ic >= IC_THRESHOLD and abs_ir >= IR_THRESHOLD:
        return "B"
    if abs_ic >= IC_THRESHOLD or abs_ir >= IR_THRESHOLD:
        return "C"
    return "D"


def compute_ic_ir(
    factor_series: pd.Series,
    return_series: pd.Series,
    fwd_days: int = DEFAULT_FWD_RETURN_DAYS,
    rolling_window: int = DEFAULT_ROLLING_WINDOW,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict:
    """对齐两个序列后计算 ts-IC + 滚动 IR + 方向性 + 评级。

    factor_series: index 为日期、value 为因子原始连续值（不是离散评分！）
    return_series: index 为日期、value 为收盘价（用于算 fwd return）
    fwd_days:      远期收益窗口
    """
    # 远期收益：r_t = close_{t+fwd} / close_t - 1
    fwd_ret = return_series.shift(-fwd_days) / return_series - 1

    df = pd.DataFrame({"f": factor_series, "r": fwd_ret}).dropna()
    n = len(df)
    if n < min_samples:
        return {
            "samples": n, "IC": None, "IR": None,
            "abs_IC": None, "abs_IR": None,
            "direction_correct": None,
            "grade": "?", "passes": False,
            "note": f"样本不足 ({n} < {min_samples})",
        }

    # 全样本 Spearman IC
    ic = float(df["f"].corr(df["r"], method="spearman"))
    if np.isnan(ic):
        return {
            "samples": n, "IC": None, "IR": None,
            "abs_IC": None, "abs_IR": None,
            "direction_correct": None,
            "grade": "?", "passes": False,
            "note": "IC 计算失败（常数序列？）",
        }

    # 滚动 IC → IR（用 Pearson 算滚动 IC，更平滑）
    rolling_ic = df["f"].rolling(rolling_window).corr(df["r"])
    rolling_ic = rolling_ic.dropna()
    if len(rolling_ic) >= 10 and rolling_ic.std() > 0:
        ir = float(rolling_ic.mean() / rolling_ic.std())
    else:
        ir = 0.0

    # 方向性：因子值高分位（top 30%） vs 低分位（bottom 30%）的平均后续收益
    q70 = df["f"].quantile(0.70)
    q30 = df["f"].quantile(0.30)
    high_ret = df.loc[df["f"] >= q70, "r"].mean()
    low_ret = df.loc[df["f"] <= q30, "r"].mean()
    if pd.isna(high_ret) or pd.isna(low_ret):
        direction_correct = None
    else:
        # 因子有效：IC > 0 时高分位收益应 > 低分位；IC < 0 时反之
        if ic > 0:
            direction_correct = bool(high_ret > low_ret)
        else:
            direction_correct = bool(low_ret > high_ret)

    abs_ic = abs(ic)
    abs_ir = abs(ir)
    grade = _grade(abs_ic, abs_ir)
    passes = grade in ("A", "B")

    return {
        "samples": n,
        "IC": round(ic, 4),
        "IR": round(ir, 4),
        "abs_IC": round(abs_ic, 4),
        "abs_IR": round(abs_ir, 4),
        "direction_correct": direction_correct,
        "high_quantile_ret": round(float(high_ret), 4) if not pd.isna(high_ret) else None,
        "low_quantile_ret": round(float(low_ret), 4) if not pd.isna(low_ret) else None,
        "grade": grade,
        "passes": passes,
        "rolling_window": rolling_window,
        "fwd_days": fwd_days,
    }


# ---------------- 因子时序计算（输入 K 线 / 估值历史，返回每日因子原始值序列） ----------------

def ts_momentum(close: pd.Series, n_days: int) -> pd.Series:
    """N 日收益率。"""
    return (close / close.shift(n_days) - 1)


def ts_volatility(close: pd.Series, n_days: int = 20) -> pd.Series:
    """N 日年化波动率。"""
    return close.pct_change().rolling(n_days).std() * np.sqrt(252)


def ts_vol_ratio(volume: pd.Series, short: int = 5, long: int = 20) -> pd.Series:
    """量比（短期均量 / 长期均量）。"""
    return volume.rolling(short).mean() / volume.rolling(long).mean()


def ts_boll_pct(close: pd.Series, n: int = 20) -> pd.Series:
    """布林位置 BB%。"""
    mid = close.rolling(n).mean()
    std = close.rolling(n).std()
    up, dn = mid + 2 * std, mid - 2 * std
    return (close - dn) / (up - dn)


def ts_pe_pct(pe_series: pd.Series, window: int = 252 * 3) -> pd.Series:
    """PE 在过去 N 日的分位（0=最低，1=最高）。"""
    def _rolling_pct(s):
        if s.dropna().empty:
            return np.nan
        v = s.iloc[-1]
        valid = s.dropna()
        return float((valid <= v).mean())
    return pe_series.rolling(window, min_periods=60).apply(_rolling_pct, raw=False)


def ts_relative_strength(stock_close: pd.Series, index_close: pd.Series,
                         n_days: int = 20) -> pd.Series:
    """个股相对指数的 N 日 RS：(stock_n_ret) - (index_n_ret)（百分点）。"""
    s_ret = (stock_close / stock_close.shift(n_days) - 1) * 100
    i_ret = (index_close / index_close.shift(n_days) - 1) * 100
    return s_ret - i_ret


# ---------------- 顶层入口 ----------------

def validate_technical_factors(kline: pd.DataFrame) -> dict[str, dict]:
    """返回 {factor_key: validation_result}。kline 已按日期升序，含 close/volume。"""
    df = kline.copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")
    close.index = pd.to_datetime(df["date"])
    volume.index = pd.to_datetime(df["date"])

    factors = {
        "tech_momentum_5d":  ts_momentum(close, 5),
        "tech_momentum_20d": ts_momentum(close, 20),
        "tech_volatility":   ts_volatility(close, 20),
        "tech_vol_ratio":    ts_vol_ratio(volume),
        "tech_boll_pct":     ts_boll_pct(close, 20),
    }
    return {k: compute_ic_ir(v, close) for k, v in factors.items()}


def validate_style_factors(value_em_df: pd.DataFrame) -> dict[str, dict]:
    """ak.stock_value_em 的 PE/PB 历史序列 → 验证 PE 分位、PB 分位。"""
    if value_em_df is None or value_em_df.empty:
        return {}
    df = value_em_df.copy()
    df["数据日期"] = pd.to_datetime(df["数据日期"], errors="coerce")
    df = df.dropna(subset=["数据日期"]).sort_values("数据日期").set_index("数据日期")

    if "当日收盘价" not in df.columns:
        return {}
    close = pd.to_numeric(df["当日收盘价"], errors="coerce")

    out: dict[str, dict] = {}
    if "PE(TTM)" in df.columns:
        pe = pd.to_numeric(df["PE(TTM)"], errors="coerce")
        out["style_pe_pct_3y"] = compute_ic_ir(ts_pe_pct(pe), close)
    if "市净率" in df.columns:
        pb = pd.to_numeric(df["市净率"], errors="coerce")
        out["style_pb_pct_3y"] = compute_ic_ir(ts_pe_pct(pb), close)
    return out


def validate_relative_strength(stock_kline: pd.DataFrame,
                               index_kline: pd.DataFrame) -> dict[str, dict]:
    """vs 上证 20 日 RS 的有效性。"""
    if index_kline is None or index_kline.empty:
        return {}
    s_close = pd.to_numeric(stock_kline["close"], errors="coerce")
    s_close.index = pd.to_datetime(stock_kline["date"])
    i_df = index_kline.copy()
    i_df["date"] = pd.to_datetime(i_df["date"])
    i_close = pd.to_numeric(i_df["close"], errors="coerce")
    i_close.index = i_df["date"]

    # 对齐日期
    common = s_close.index.intersection(i_close.index)
    if len(common) < 100:
        return {}
    s_close = s_close.loc[common]
    i_close = i_close.loc[common]

    rs = ts_relative_strength(s_close, i_close, 20)
    return {"rs_vs_index_20d": compute_ic_ir(rs, s_close)}


def summarize_validation(results: dict[str, dict]) -> dict:
    """汇总：通过的因子数 / 总数 / 平均 IC/IR / 各等级数量。"""
    if not results:
        return {"total": 0, "passed": 0, "grades": {}}
    grades = {"A": 0, "B": 0, "C": 0, "D": 0, "?": 0}
    passed = 0
    abs_ics = []
    abs_irs = []
    for r in results.values():
        g = r.get("grade", "?")
        grades[g] = grades.get(g, 0) + 1
        if r.get("passes"):
            passed += 1
        if r.get("abs_IC") is not None:
            abs_ics.append(r["abs_IC"])
        if r.get("abs_IR") is not None:
            abs_irs.append(r["abs_IR"])
    return {
        "total": len(results),
        "passed": passed,
        "passed_ratio": round(passed / len(results), 3),
        "grades": grades,
        "mean_abs_IC": round(float(np.mean(abs_ics)), 4) if abs_ics else None,
        "mean_abs_IR": round(float(np.mean(abs_irs)), 4) if abs_irs else None,
    }
