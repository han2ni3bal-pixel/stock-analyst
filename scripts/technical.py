"""技术指标计算 — MA / RSI / Bollinger / MACD / 量比。

输入：日 K DataFrame（必须含列 date, open, high, low, close, volume）
输出：附加技术指标列 + 信号字典
"""
from __future__ import annotations

import pandas as pd
import numpy as np


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("date").reset_index(drop=True).copy()
    close = df["close"].astype(float)

    for n in (5, 10, 20, 60):
        df[f"MA{n}"] = close.rolling(n).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI14"] = 100 - 100 / (1 + rs)

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    df["BB_mid"] = mid
    df["BB_up"] = mid + 2 * std
    df["BB_dn"] = mid - 2 * std
    df["BB_pct"] = (close - df["BB_dn"]) / (df["BB_up"] - df["BB_dn"])

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["MACD"] - df["MACD_signal"]

    df["VOL_MA5"] = df["volume"].rolling(5).mean()
    df["VOL_RATIO"] = df["volume"] / df["VOL_MA5"]

    return df


def analyze_signals(df: pd.DataFrame, target_date: str) -> dict:
    """target_date: 'YYYY-MM-DD'"""
    df = df.copy()
    df["date"] = df["date"].astype(str)
    idx = df.index[df["date"] == target_date]
    if len(idx) == 0:
        return {"score": 0.0, "signals": ["目标日无数据"], "snapshot": {}}

    i = idx[0]
    row = df.iloc[i]
    score = 0.0
    signals: list[str] = []
    close = float(row["close"])

    snapshot = {k: (float(row[k]) if pd.notna(row[k]) else None)
                for k in ["close", "MA5", "MA10", "MA20", "MA60", "RSI14",
                         "BB_up", "BB_mid", "BB_dn", "BB_pct", "MACD_hist", "VOL_RATIO"]}

    if all(snapshot[k] is not None for k in ("MA5", "MA10", "MA20", "MA60")):
        if snapshot["MA5"] > snapshot["MA10"] > snapshot["MA20"] > snapshot["MA60"]:
            signals.append("均线多头排列(MA5>10>20>60) → +1")
            score += 1
        elif snapshot["MA5"] < snapshot["MA10"] < snapshot["MA20"] < snapshot["MA60"]:
            signals.append("均线空头排列 → -1")
            score -= 1
        elif close > snapshot["MA20"]:
            signals.append(f"价格在MA20({snapshot['MA20']:.2f})之上 → +0.3")
            score += 0.3
        elif close < snapshot["MA20"]:
            signals.append(f"价格跌破MA20({snapshot['MA20']:.2f}) → -0.3")
            score -= 0.3

    rsi = snapshot["RSI14"]
    if rsi is not None:
        if rsi >= 80:
            signals.append(f"RSI={rsi:.1f} 严重超买 → -1"); score -= 1
        elif rsi >= 70:
            signals.append(f"RSI={rsi:.1f} 超买 → -0.5"); score -= 0.5
        elif rsi <= 20:
            signals.append(f"RSI={rsi:.1f} 严重超卖 → +1"); score += 1
        elif rsi <= 30:
            signals.append(f"RSI={rsi:.1f} 超卖 → +0.5"); score += 0.5
        elif 50 < rsi < 70:
            signals.append(f"RSI={rsi:.1f} 偏强 → +0.2"); score += 0.2
        elif 30 < rsi < 50:
            signals.append(f"RSI={rsi:.1f} 偏弱 → -0.2"); score -= 0.2

    bbp = snapshot["BB_pct"]
    if bbp is not None:
        if bbp >= 1:
            signals.append(f"突破布林上轨(BB%={bbp:.2f}) → 强势但有回吐风险 -0.3"); score -= 0.3
        elif bbp >= 0.8:
            signals.append(f"接近布林上轨(BB%={bbp:.2f}) → +0.3"); score += 0.3
        elif bbp <= 0:
            signals.append(f"跌破布林下轨(BB%={bbp:.2f}) → 超卖反弹 +0.3"); score += 0.3
        elif bbp <= 0.2:
            signals.append(f"接近布林下轨(BB%={bbp:.2f}) → -0.3"); score -= 0.3

    hist = snapshot["MACD_hist"]
    if hist is not None and i > 0:
        prev_hist = df.iloc[i - 1]["MACD_hist"]
        if pd.notna(prev_hist):
            if prev_hist <= 0 < hist:
                signals.append("MACD金叉 → +0.5"); score += 0.5
            elif prev_hist >= 0 > hist:
                signals.append("MACD死叉 → -0.5"); score -= 0.5
            elif hist > 0 and hist > prev_hist:
                signals.append(f"MACD柱放大({hist:.3f}) → +0.2"); score += 0.2
            elif hist < 0 and hist < prev_hist:
                signals.append(f"MACD柱缩小转负({hist:.3f}) → -0.2"); score -= 0.2

    vr = snapshot["VOL_RATIO"]
    if vr is not None and i > 0:
        prev_close = float(df.iloc[i - 1]["close"])
        pct = (close - prev_close) / prev_close * 100
        if vr >= 1.5 and pct >= 2:
            signals.append(f"放量上涨(量比{vr:.2f}, +{pct:.1f}%) → +0.5"); score += 0.5
        elif vr >= 1.5 and pct <= -2:
            signals.append(f"放量下跌(量比{vr:.2f}, {pct:.1f}%) → -0.5"); score -= 0.5
        elif vr < 0.7 and pct > 0:
            signals.append(f"缩量上涨(量比{vr:.2f}) → 持续性弱 +0.1"); score += 0.1

    return {"score": score, "signals": signals, "snapshot": snapshot}
