"""快速探测当前核心数据源可用性。

当前策略：
- Tickflow: 行情 / K 线 / 标的信息主源
- yfinance: 美股盘前 / 盘后数据
- akshare: A 股 / 港股新闻
- Finnhub: 美股新闻
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from data_layer import (  # noqa: E402
    _apply_proxy_for_yfinance,
    _finnhub_token,
    _tickflow_client,
    _tickflow_symbol,
    fetch_info,
    fetch_kline,
    fetch_news,
)


def _probe(name: str, fn):
    t0 = time.time()
    try:
        res = fn()
        if hasattr(res, "empty"):
            ok = not res.empty
            detail = f"rows={len(res)}"
        elif isinstance(res, (list, tuple, dict, str)):
            ok = bool(res)
            detail = f"items={len(res)}" if not isinstance(res, str) else "ok"
        else:
            ok = res is not None
            detail = "ok" if ok else "empty"
        mark = "✅" if ok else "⚪"
        print(f"{mark} {name:34s} {detail:<12s} ({time.time() - t0:.1f}s)")
    except Exception as e:
        print(f"❌ {name:34s} {type(e).__name__}: {str(e)[:80]}")


def main():
    today = datetime.today().strftime("%Y%m%d")
    start = (datetime.today() - timedelta(days=7)).date().isoformat()
    end = datetime.today().date().isoformat()

    _probe("tickflow client", _tickflow_client)
    _probe("tickflow symbol A", lambda: _tickflow_symbol("astock", "600000", "sh"))
    _probe("tickflow info A", lambda: fetch_info("astock", "600000", "sh"))
    _probe("tickflow kline A", lambda: fetch_kline("astock", "600000", "sh", today, lookback=30))
    _probe("tickflow info US", lambda: fetch_info("us", "AAPL", "us"))
    _probe("tickflow kline US", lambda: fetch_kline("us", "AAPL", "us", today, lookback=30))

    def yfinance_prepost():
        _apply_proxy_for_yfinance()
        import yfinance as yf
        return yf.Ticker("AAPL").history(period="1d", interval="1m", prepost=True)

    _probe("yfinance prepost US", yfinance_prepost)
    _probe("akshare news A", lambda: fetch_news("astock", "600000", "浦发银行"))

    def finnhub_news():
        import requests
        token = _finnhub_token()
        if not token:
            return []
        resp = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": "AAPL", "from": start, "to": end, "token": token},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    _probe("finnhub news US", finnhub_news)


if __name__ == "__main__":
    main()
