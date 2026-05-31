"""个股资金流向 — 多源回退。

被风控较严的：push2.eastmoney.com（akshare.stock_individual_fund_flow 默认走这里）
本模块按可靠度尝试：
  1. 东财 push2his.eastmoney.com — 历史日资金流（含主力/超大单/大单）
  2. 同花顺 data.10jqka.com.cn — 全市场即时榜（akshare.stock_fund_flow_individual）
  3. 雪球 stock.xueqiu.com — 分钟级，需 cookie；自动 GET 首页拿 token
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
import requests


def from_eastmoney_push2his(code: str, market: int = 1, days: int = 20) -> Optional[pd.DataFrame]:
    """market: 1=沪 / 0=深"""
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "lmt": days,
        "klt": 101,
        "secid": f"{market}.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    }
    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200 or not r.text.strip():
        return None
    body = r.json()
    if body.get("rc") != 0 or not body.get("data"):
        return None
    rows = []
    for line in body["data"]["klines"]:
        p = line.split(",")
        rows.append({
            "date": p[0],
            "main_net": float(p[1]),
            "small_net": float(p[2]),
            "medium_net": float(p[3]),
            "large_net": float(p[4]),
            "xlarge_net": float(p[5]),
            "main_pct": float(p[6]),
            "close": float(p[11]),
            "pct_change": float(p[12]),
        })
    return pd.DataFrame(rows)


def from_ths_via_akshare(code: str) -> Optional[pd.DataFrame]:
    import akshare as ak
    df = ak.stock_fund_flow_individual(symbol="即时")
    if df is None or df.empty:
        return None
    sub = df[df["股票代码"].astype(str).str.zfill(6) == code]
    return sub if not sub.empty else None


def from_xueqiu(code: str, market_prefix: str = "SH", count: int = 30) -> Optional[pd.DataFrame]:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": f"https://xueqiu.com/S/{market_prefix}{code}",
    })
    sess.get("https://xueqiu.com/", timeout=10)
    r = sess.get(
        "https://stock.xueqiu.com/v5/stock/capital/flow.json",
        params={"symbol": f"{market_prefix}{code}", "count": count, "extend": "detail"},
        timeout=10,
    )
    body = r.json()
    if body.get("error_code"):
        return None
    items = body.get("data", {}).get("items") or []
    if not items:
        return None
    return pd.DataFrame(items)


def fetch_with_fallback(code: str, market: str = "sh") -> tuple[pd.DataFrame | None, str | None]:
    """按顺序尝试三源，返回 (df, 来源名)"""
    market_id = 1 if market == "sh" else 0
    market_prefix = market.upper()
    sources = [
        ("eastmoney push2his", lambda: from_eastmoney_push2his(code, market_id, 20)),
        ("同花顺(akshare)",     lambda: from_ths_via_akshare(code)),
        ("雪球",                 lambda: from_xueqiu(code, market_prefix, 30)),
    ]
    for name, fn in sources:
        try:
            df = fn()
        except Exception:
            continue
        if df is not None and not df.empty:
            return df, name
    return None, None


def _parse_cn_amount(s) -> float | None:
    """解析 '2.50亿' / '1234万' / '1234' / 数值 → 元。"""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace(",", "")
    if not s or s in ("-", "--"):
        return None
    try:
        if s.endswith("亿"):
            return float(s[:-1]) * 1e8
        if s.endswith("万"):
            return float(s[:-1]) * 1e4
        return float(s)
    except ValueError:
        return None


def extract_main_net(df: pd.DataFrame, source: str, target_dash: str) -> float | None:
    """从结果 df 中提取目标日主力净额（元）。"""
    if "push2his" in source:
        sub = df[df["date"] == target_dash]
        if not sub.empty:
            return float(sub.iloc[0]["main_net"])

    elif "同花顺" in source:
        # 即时榜：净额列可能是 '2.50亿' 这种字符串
        net_col = next((c for c in df.columns if "净额" in c), None)
        if net_col and not df.empty:
            return _parse_cn_amount(df.iloc[0][net_col])

    elif "雪球" in source:
        if "timestamp" in df.columns and "main_net_inflows" in df.columns:
            ts = pd.to_datetime(df["timestamp"], unit="ms")
            today = df[ts.dt.strftime("%Y-%m-%d") == target_dash]
            if not today.empty:
                return float(today["main_net_inflows"].sum())
    return None
