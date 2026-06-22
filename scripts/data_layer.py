"""按 kind 分派数据获取 — A 股 / 港股 / ETF / 美股。

当前数据源策略：
- 行情 / K 线 / 基本信息：Tickflow 作为第一股市信息源。
- 美股实时：盘中使用 Tickflow；盘前 / 盘后使用 yfinance prepost 数据。
- 新闻：A 股用 akshare；美股用 Finnhub；港股沿用 akshare；主源为空时用 Claude LLM 兜底。
- 其他旧行情源（东财 / 新浪 / 雪球 / itick 等）不再作为 fallback。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable, Optional, TypeVar

import akshare as ak
import pandas as pd

from llm_client import LLMError, generate_text, get_llm_status, parse_json_text

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------- 代理管理（移植自 tradeHelper） ----------------

_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
)
_PROXY_STATE: dict = {"applied": None}


@contextmanager
def _without_system_proxy():
    """临时清除进程级代理环境变量，避免国内 akshare 接口被 macOS 系统代理误伤。"""
    saved = {k: os.environ[k] for k in _PROXY_ENV_KEYS if k in os.environ}
    for k in _PROXY_ENV_KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k in _PROXY_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(saved)


def _detect_macos_http_proxy() -> str:
    """读取 macOS 系统 HTTP 代理（用户开了系统代理时）。"""
    try:
        out = subprocess.check_output(["scutil", "--proxy"], text=True, timeout=3)
        if "HTTPEnable : 1" not in out and "HTTPSEnable : 1" not in out:
            return ""
        host_m = re.search(r"HTTPProxy : (\S+)", out)
        port_m = re.search(r"HTTPPort : (\d+)", out)
        if host_m and port_m:
            return f"http://{host_m.group(1)}:{port_m.group(1)}"
    except Exception:
        pass
    return ""


def _resolve_proxy_url() -> str:
    """优先用环境变量 STOCK_ANALYST_PROXY，否则回退到 macOS 系统 HTTP 代理。"""
    configured = (os.environ.get("STOCK_ANALYST_PROXY", "") or "").strip()
    if configured:
        return configured
    return _detect_macos_http_proxy()


def _yfinance_proxy(proxy_url: str):
    """yfinance >= 1.4 (curl_cffi) 要求 proxy 为 dict。"""
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _apply_proxy_for_yfinance():
    """把代理注入 yfinance（跨境抓 Yahoo Finance 数据）。"""
    proxy_url = _resolve_proxy_url()
    if _PROXY_STATE["applied"] == proxy_url:
        return
    try:
        import yfinance as yf
        yf.config.network.proxy = _yfinance_proxy(proxy_url)
        _PROXY_STATE["applied"] = proxy_url
        if proxy_url:
            print(f"  [代理] yfinance 走 {proxy_url}")
    except Exception as e:
        logger.warning(f"yfinance proxy 配置失败: {e}")


def _retry(func: Callable[[], T], max_retries: int = 3, label: str = "") -> T:
    """带退避的网络调用重试。"""
    delay = 2.0
    last_exc = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exc = e
            msg = str(e)
            transient = any(k in msg for k in [
                "Rate limited", "Too Many Requests", "rate limited",
                "RemoteDisconnected", "Connection aborted", "EOF occurred",
                "ProxyError", "Empty reply",
            ])
            if transient and attempt < max_retries - 1:
                time.sleep(delay)
                delay = min(delay * 1.5, 15)
                continue
            raise
    if last_exc:
        raise last_exc


# ---------------- 列名标准化 ----------------

_COL_MAP_CN = {
    "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
    "收盘": "close", "成交量": "volume", "成交额": "amount",
}

_COL_MAP_EN = {
    "Date": "date", "Open": "open", "High": "high", "Low": "low",
    "Close": "close", "Volume": "volume",
}


def normalize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """K 线 DataFrame 统一成 date(str) / open / high / low / close / volume(float)。"""
    df = df.rename(columns={**_COL_MAP_CN, **_COL_MAP_EN}).copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    keep = ["date", "open", "high", "low", "close", "volume"]
    return df[keep].sort_values("date").reset_index(drop=True)


# ---------------- Tickflow 行情主源 ----------------

_TICKFLOW_DEFAULT_TOKEN = "tk_1ee21297714f43aba88e7c7ede4e8645"
_TICKFLOW_CLIENT = None

# Finnhub 美股新闻源。按用户要求内置；也允许用环境变量覆盖。
_FINNHUB_DEFAULT_TOKEN = "d8m1pk9r01qkiso51n1gd8m1pk9r01qkiso51n20"
_FINNHUB_BASE_URL = "https://finnhub.io/api/v1"


def _tickflow_token() -> str:
    return (os.environ.get("STOCK_ANALYST_TICKFLOW_TOKEN", "") or _TICKFLOW_DEFAULT_TOKEN).strip()


def _finnhub_token() -> str:
    return (os.environ.get("STOCK_ANALYST_FINNHUB_TOKEN", "") or _FINNHUB_DEFAULT_TOKEN).strip()


def _tickflow_client():
    """Tickflow 客户端：优先尝试 token，失败则使用免费服务。"""
    global _TICKFLOW_CLIENT
    if _TICKFLOW_CLIENT is not None:
        return _TICKFLOW_CLIENT
    from tickflow import TickFlow
    token = _tickflow_token()
    if token:
        for factory in (
            lambda: TickFlow(api_key=token),
            lambda: TickFlow(token=token),
            lambda: TickFlow.default(api_key=token),
        ):
            try:
                _TICKFLOW_CLIENT = factory()
                return _TICKFLOW_CLIENT
            except Exception:
                continue
    _TICKFLOW_CLIENT = TickFlow.free()
    return _TICKFLOW_CLIENT


def _tickflow_symbol(kind: str, code: str, market: str = "") -> str:
    if kind == "us":
        code_u = code.upper()
        return code_u if "." in code_u else f"{code_u}.US"
    if kind == "hk":
        return f"{str(code).lstrip('0') or '0'}.HK"
    suffix = "SH" if (market == "sh" or str(code).startswith(("5", "6", "9"))) else "SZ"
    return f"{code}.{suffix}"


def _normalize_tickflow_kline(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    renamed = df.rename(columns={
        "trade_date": "date", "datetime": "date", "time": "date", "ts": "date",
        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
        "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
    }).copy()
    if "date" not in renamed.columns and "timestamp" in renamed.columns:
        renamed["date"] = pd.to_datetime(renamed["timestamp"], unit="ms", errors="coerce")
    if "date" not in renamed.columns and isinstance(renamed.index, pd.DatetimeIndex):
        renamed = renamed.reset_index().rename(columns={renamed.index.name or "index": "date"})
    return normalize_kline(renamed)


def _fetch_kline_tickflow(kind: str, code: str, market: str, s_dash: str,
                          e_dash: str, lookback: int) -> Optional[pd.DataFrame]:
    symbol = _tickflow_symbol(kind, code, market)
    try:
        df = _tickflow_client().klines.get(symbol, period="1d", count=max(lookback * 2, 120), as_dataframe=True)
    except Exception as e:
        print(f"  [Tickflow K 线失败] {type(e).__name__}: {str(e)[:100]}")
        return None
    if df is None or df.empty:
        return None
    try:
        out = _normalize_tickflow_kline(df)
        out = out[(out["date"] >= s_dash) & (out["date"] <= e_dash)]
        if out.empty:
            return None
        print(f"  ✓ Tickflow K 线 {symbol} {len(out)} 条")
        return out
    except Exception as e:
        print(f"  [Tickflow K 线格式异常] {type(e).__name__}: {str(e)[:100]}")
        return None


def _fetch_info_tickflow(kind: str, code: str, market: str) -> Optional[pd.DataFrame]:
    symbol = _tickflow_symbol(kind, code, market)
    try:
        instruments = _tickflow_client().instruments.batch(symbols=[symbol])
    except Exception as e:
        print(f"  [Tickflow 标的信息失败] {type(e).__name__}: {str(e)[:100]}")
        return None
    if not instruments:
        return None
    inst = instruments[0]
    keep = [
        ("名称", inst.get("name")),
        ("代码", inst.get("symbol") or symbol),
        ("行业", inst.get("industry") or inst.get("sector")),
        ("交易所", inst.get("exchange") or inst.get("market")),
        ("币种", inst.get("currency")),
        ("类型", inst.get("type")),
        ("简介", str(inst.get("description") or "")[:500]),
    ]
    rows = [(k, v) for k, v in keep if v not in (None, "", 0)]
    if not rows:
        return None
    print(f"  ✓ Tickflow 标的信息 {symbol}")
    return pd.DataFrame(rows, columns=["item", "value"])


def _fetch_realtime_tickflow(kind: str, code: str, market: str = "") -> Optional[dict]:
    symbol = _tickflow_symbol(kind, code, market)
    try:
        df = _tickflow_client().klines.get(symbol, period="1m", count=1, as_dataframe=True)
    except Exception:
        try:
            df = _tickflow_client().klines.get(symbol, period="1d", count=1, as_dataframe=True)
        except Exception as e:
            logger.warning(f"Tickflow quote 失败 {symbol}: {e}")
            return None
    if df is None or df.empty:
        return None
    try:
        row = _normalize_tickflow_kline(df).iloc[-1]
        prev_close = float(row.get("close")) if row.get("close") is not None else None
        return {
            "price": row.get("close"), "open": row.get("open"), "high": row.get("high"),
            "low": row.get("low"), "change": None, "change_pct": None,
            "volume": row.get("volume"), "turnover": None, "ts_ms": None,
            "region": kind, "source": "tickflow", "prev_close": prev_close,
        }
    except Exception as e:
        logger.warning(f"Tickflow quote 格式异常 {symbol}: {e}")
        return None


def _fetch_realtime_us_yfinance(code: str) -> Optional[dict]:
    try:
        _apply_proxy_for_yfinance()
        import yfinance as yf
        df = _retry(lambda: yf.Ticker(code.upper()).history(period="1d", interval="1m", prepost=True),
                    max_retries=2, label="yf.prepost")
    except Exception as e:
        logger.warning(f"yfinance prepost 失败 {code}: {e}")
        return None
    if df is None or df.empty:
        return None
    try:
        row = df.reset_index().iloc[-1]
        return {
            "price": row.get("Close"), "open": row.get("Open"), "high": row.get("High"),
            "low": row.get("Low"), "change": None, "change_pct": None,
            "volume": row.get("Volume"), "turnover": None, "ts_ms": None,
            "region": "us", "source": "yfinance-prepost",
        }
    except Exception:
        return None


def _is_us_regular_session_now() -> bool:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return False
    if now.weekday() >= 5:
        return False
    t = now.time()
    return t >= datetime.strptime("09:30", "%H:%M").time() and t <= datetime.strptime("16:00", "%H:%M").time()


def fetch_realtime(kind: str, code: str, market: str = "") -> Optional[dict]:
    """实时报价：美股盘中用 Tickflow；盘前 / 盘后用 yfinance prepost。"""
    if kind == "us" and not _is_us_regular_session_now():
        yf_quote = _fetch_realtime_us_yfinance(code)
        if yf_quote is not None:
            return yf_quote
    return _fetch_realtime_tickflow(kind, code, market)


# ---------------- 基本信息 ----------------

def fetch_info(kind: str, code: str, market: str) -> Optional[pd.DataFrame]:
    """返回 (item, value) 两列的 DataFrame。"""
    return _fetch_info_tickflow(kind, code, market)


def _fetch_info_us(code: str) -> Optional[pd.DataFrame]:
    """兼容旧调用：美股基本信息统一走 Tickflow。"""
    return _fetch_info_tickflow("us", code, "us")


# ---------------- K 线 ----------------

def fetch_kline(kind: str, code: str, market: str, target_date: str,
                lookback: int = 120) -> Optional[pd.DataFrame]:
    """target_date: YYYYMMDD。返回标准化 K 线（按日期升序）。"""
    end = datetime.strptime(target_date, "%Y%m%d") + timedelta(days=2)
    start = end - timedelta(days=lookback * 2)
    s_dash, e_dash = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    return _fetch_kline_tickflow(kind, code, market, s_dash, e_dash, lookback)


def _fetch_kline_us(code: str, s_str: str, e_str: str,
                    s_dash: str, e_dash: str) -> Optional[pd.DataFrame]:
    """兼容旧调用：美股 K 线统一走 Tickflow。"""
    return _fetch_kline_tickflow("us", code, "us", s_dash, e_dash, 120)


# ---------------- 新闻 ----------------

def fetch_news(kind: str, code: str, name: str = "") -> Optional[pd.DataFrame]:
    """ETF 没有公司新闻；A 股/港股用 akshare；美股用 Finnhub。

    任一主源取空/异常都会再走一次 LLM 兜底（Claude 列出近期新闻）。
    `name` 仅在 LLM 兜底时用作 prompt 上下文，主源不依赖。
    """
    if kind == "etf":
        return None
    df: Optional[pd.DataFrame] = None
    try:
        if kind == "us":
            df = _fetch_news_us(code)
        else:
            with _without_system_proxy():
                df = ak.stock_news_em(symbol=code)
    except Exception as e:
        print(f"  [新闻失败] {type(e).__name__}: {str(e)[:100]}")
        df = None

    if df is not None and not df.empty:
        return df

    # 兜底：让 Claude 列近期新闻
    print("  [新闻] 主源为空，尝试 LLM 兜底…")
    return _fetch_news_via_llm(code, name=name, market=kind)


_LLM_NEWS_PROMPT_CN = """你是一位专业的财经新闻编辑。请列出关于 {name}（{code}）近一周的真实新闻。

返回 {limit} 条，按日期从新到旧排列。每条包含：日期(YYYY-MM-DD HH:MM:SS)、标题、内容摘要、来源。
请用中文输出。**只输出 JSON 数组**，不要其他任何前后文字：

[
  {{"date": "2026-05-27 09:00:00", "title": "新闻标题", "content": "新闻内容摘要（200 字以内）", "source": "东方财富"}}
]"""

_LLM_NEWS_PROMPT_EN = """You are a professional financial news editor. List real recent news about {name} ({code}) from the past week.

Return {limit} items sorted by date descending. Each item: date (YYYY-MM-DD HH:MM:SS), title, content summary, source.
Output in English. **JSON array only**, nothing else:

[
  {{"date": "2026-05-27 09:00:00", "title": "News Title", "content": "News content summary (under 200 words)", "source": "Reuters"}}
]"""


def _fetch_news_via_llm(code: str, name: str = "", market: str = "astock",
                        limit: int = 8) -> Optional[pd.DataFrame]:
    """可选 LLM 新闻兜底，转成与主新闻源兼容的列名。"""
    status = get_llm_status()
    if not status.available:
        logger.info("跳过 LLM 新闻兜底：%s", status.reason)
        return None

    label = name or code
    prompt = (_LLM_NEWS_PROMPT_EN if market == "us" else _LLM_NEWS_PROMPT_CN).format(
        name=label, code=code, limit=limit,
    )

    try:
        response = generate_text(
            prompt,
            max_tokens=2048,
            retries=2,
        )
    except LLMError as e:
        logger.warning(f"LLM 新闻兜底调用失败: {e}")
        return None

    try:
        items = parse_json_text(response.text)
    except (ValueError, TypeError):
        logger.warning(f"LLM 新闻 JSON 解析失败: {response.text[:200]}")
        return None

    rows = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        rows.append({
            "标题": str(item.get("title", "")),
            "发布时间": str(item.get("date", ""))[:19],
            "文章来源": str(item.get("source", "LLM")),
            "内容": str(item.get("content", "")),
        })
    if not rows:
        return None
    print(f"  ✓ LLM 新闻兜底 {len(rows)} 条（{response.provider}/{response.model}）")
    return pd.DataFrame(rows)


def _fetch_news_us(code: str, limit: int = 30) -> Optional[pd.DataFrame]:
    """Finnhub company-news → 转成与东财格式兼容的 DataFrame（列名：标题/发布时间/文章来源/内容）。"""
    token = _finnhub_token()
    if not token:
        return None
    import requests
    end = datetime.today().date()
    start = end - timedelta(days=7)
    try:
        resp = requests.get(
            f"{_FINNHUB_BASE_URL}/company-news",
            params={"symbol": code.upper(), "from": start.isoformat(), "to": end.isoformat(), "token": token},
            timeout=20,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        logger.warning(f"Finnhub news 失败 {code}: {e}")
        return None
    if not raw:
        return None
    rows = []
    for item in raw[:limit]:
        ts_raw = item.get("datetime")
        try:
            ts = datetime.fromtimestamp(int(ts_raw)).strftime("%Y-%m-%d %H:%M:%S") if ts_raw else ""
        except Exception:
            ts = ""
        rows.append({
            "标题": item.get("headline", ""),
            "发布时间": ts,
            "文章来源": item.get("source", "Finnhub"),
            "内容": item.get("summary", ""),
        })
    if not rows:
        return None
    print(f"  ✓ Finnhub company-news {len(rows)} 条")
    return pd.DataFrame(rows)


# ---------------- 资金流（仅 A 股 / ETF） ----------------

def fetch_etf_fund_flow(code: str, target_dash: str) -> Optional[float]:
    """ETF 资金流旧外部源已禁用。"""
    return None


def fetch_southbound_summary() -> Optional[pd.DataFrame]:
    """南向资金旧外部源已禁用。"""
    return None
