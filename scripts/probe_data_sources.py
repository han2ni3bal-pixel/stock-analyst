"""快速探测 akshare 关键接口可用性 — 当某些数据获取失败时用来诊断。

只跑分析所必需的几个接口，约 20-30s。
"""
from __future__ import annotations

import time
import akshare as ak

PROBES = [
    ("stock_individual_basic_info_xq",  lambda: ak.stock_individual_basic_info_xq(symbol="SH603893")),
    ("stock_zh_a_daily(sina)",           lambda: ak.stock_zh_a_daily(symbol="sh603893",
                                                                      start_date="20260101",
                                                                      end_date="20260524",
                                                                      adjust="qfq")),
    ("stock_news_em",                    lambda: ak.stock_news_em(symbol="603893")),
    ("stock_lhb_detail_em",              lambda: ak.stock_lhb_detail_em(start_date="20260518",
                                                                         end_date="20260522")),
    ("stock_fund_flow_individual",       lambda: ak.stock_fund_flow_individual(symbol="即时")),
]


def main():
    for name, fn in PROBES:
        t0 = time.time()
        try:
            df = fn()
            n = 0 if df is None else len(df)
            elapsed = time.time() - t0
            mark = "✅" if n > 0 else "⚪"
            print(f"{mark} {name:42s} rows={n:<6}  ({elapsed:.1f}s)")
        except Exception as e:
            print(f"❌ {name:42s} {type(e).__name__}: {str(e)[:60]}")


if __name__ == "__main__":
    main()
