"""按 kind 分派数据获取 — A 股 / 港股 / ETF / 美股。

各源调用细节：
- A 股 K 线  : ak.stock_zh_a_daily(symbol='sh603893', adjust='qfq')   新浪
- 港股 K 线 : ak.stock_hk_daily(symbol='01810', adjust='qfq')          雪球
- ETF K 线  : ak.fund_etf_hist_em(symbol='510300', adjust='qfq')      东财 datacenter
- 美股 K 线 : ak.stock_us_hist(symbol='105.AAPL', adjust='qfq')        东财，secid 前缀 105/106/107 任一
              P1 fallback: yfinance.Ticker(code).history(...)（需走代理）
              P2 fallback: itick /stock/kline (需 STOCK_ANALYST_ITICK_TOKEN，付费源不走代理)

新闻：A 股 / 港股 走东财 ak.stock_news_em；美股走 yfinance.Ticker.news；
      所有市场 fallback：调 Claude（联网或非联网都跑一次）让 LLM 列出 5-10 条近期新闻。

⚠️  跨境数据源（yfinance / Yahoo Finance）受网络环境影响，**调用前需要应用代理**；
    国内源（akshare 中转的 sina / xueqiu / 东财）相反，必须**清空进程级代理**避免被 macOS 系统代理误伤。
    itick 直连 api0.itick.org，session.trust_env=False 不受系统代理污染。
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


# ---------------- itick 付费数据源（美股 K 线 + info 兜底） ----------------

_ITICK_BASE_URL = "https://api0.itick.org"
_ITICK_SESSION = None


def _itick_token() -> str:
    return (os.environ.get("STOCK_ANALYST_ITICK_TOKEN", "") or "").strip()


def _itick_session():
    """itick 直连：trust_env=False 不走系统代理，避免被国内代理打回去。"""
    global _ITICK_SESSION
    if _ITICK_SESSION is not None:
        return _ITICK_SESSION
    import requests
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"accept": "application/json", "token": _itick_token()})
    _ITICK_SESSION = s
    return s


def _itick_get(path: str, params: dict, max_retries: int = 3) -> dict:
    """带重试的 itick GET；非 0 code 抛 RuntimeError。"""
    if not _itick_token():
        raise RuntimeError("STOCK_ANALYST_ITICK_TOKEN 未配置")
    url = f"{_ITICK_BASE_URL}{path}"
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = _itick_session().get(url, params=params, timeout=30)
            if not resp.ok:
                logger.warning(f"itick HTTP {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"itick code={data.get('code')} msg={data.get('msg')}")
            return data
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 10)
    raise last_exc  # type: ignore[misc]


def _fetch_kline_us_itick(code: str, s_dash: str, e_dash: str) -> Optional[pd.DataFrame]:
    """itick 美股日 K 线。kType=8 = daily。"""
    if not _itick_token():
        return None
    try:
        d_start = datetime.strptime(s_dash, "%Y-%m-%d")
        d_end = datetime.strptime(e_dash, "%Y-%m-%d")
        days = max((d_end - d_start).days, 1)
        limit = min(int(days * 1.4), 2000)
    except Exception:
        limit = 1000
    try:
        data = _itick_get("/stock/kline", {
            "region": "US", "code": code.upper(), "kType": 8, "limit": limit,
        })
    except Exception as e:
        logger.warning(f"itick kline 失败 {code}: {e}")
        return None
    bars = data.get("data") or []
    if not bars:
        return None
    rows = []
    for b in bars:
        ts = b.get("t", 0)
        try:
            d = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d")
        except Exception:
            continue
        if d < s_dash or d > e_dash:
            continue
        rows.append({
            "date": d,
            "open": b.get("o"), "high": b.get("h"),
            "low":  b.get("l"), "close": b.get("c"),
            "volume": b.get("v"),
        })
    if not rows:
        return None
    print(f"  ✓ itick /stock/kline (US {code}) {len(rows)} 条")
    return normalize_kline(pd.DataFrame(rows))


def _fetch_info_us_itick(code: str) -> Optional[pd.DataFrame]:
    """itick 美股 info → 转成 (item, value) 与 yfinance 输出对齐。"""
    if not _itick_token():
        return None
    try:
        data = _itick_get("/stock/info", {"type": "stock", "region": "US", "code": code.upper()})
    except Exception as e:
        logger.warning(f"itick info 失败 {code}: {e}")
        return None
    d = data.get("data") or {}
    if not d:
        return None
    keep = [
        ("名称", d.get("n")),
        ("代码", code.upper()),
        ("行业", d.get("i") or d.get("s")),
        ("交易所", d.get("e")),
        ("简介", str(d.get("bd") or "")[:500]),
    ]
    rows = [(k, v) for k, v in keep if v not in (None, "", 0)]
    if not rows:
        return None
    print(f"  ✓ itick /stock/info (US {code})")
    return pd.DataFrame(rows, columns=["item", "value"])


# ---------------- 美股 secid 候选 ----------------

def _us_secid_candidates(code: str) -> list[str]:
    """东财美股 hist 接口的 secid 候选，形如 105.AAPL / 106.AAPL / 107.AAPL。

    105 = NASDAQ, 106 = NYSE, 107 = AMEX；同一 code 可能仅其中一个能命中。
    """
    code = code.upper()
    if "." in code:
        return [code]
    return [f"{p}.{code}" for p in ("105", "106", "107")]


# ---------------- 基本信息 ----------------

def fetch_info(kind: str, code: str, market: str) -> Optional[pd.DataFrame]:
    """返回 (item, value) 两列的 DataFrame。"""
    try:
        if kind == "astock":
            sym = f"{market.upper()}{code}"
            with _without_system_proxy():
                return ak.stock_individual_basic_info_xq(symbol=sym)
        if kind == "hk":
            with _without_system_proxy():
                return ak.stock_individual_basic_info_xq(symbol=f"HK{code}")
        if kind == "etf":
            with _without_system_proxy():
                spot = ak.fund_etf_spot_em()
            sub = spot[spot["代码"].astype(str) == code]
            if sub.empty:
                return None
            row = sub.iloc[0]
            keep = ["代码", "名称", "最新价", "涨跌幅", "成交额", "换手率",
                    "流通市值", "总市值", "最新份额", "数据日期"]
            data = [(k, row.get(k)) for k in keep if k in row]
            return pd.DataFrame(data, columns=["item", "value"])
        if kind == "us":
            return _fetch_info_us(code)
    except Exception as e:
        print(f"  [info 失败] {type(e).__name__}: {str(e)[:100]}")
    return None


def _fetch_info_us(code: str) -> Optional[pd.DataFrame]:
    """美股基本信息：先试 yfinance.Ticker.info（走代理），失败再降级到 ak.stock_individual_basic_info_us_xq。"""
    code_u = code.upper()
    # P0: yfinance
    try:
        _apply_proxy_for_yfinance()
        import yfinance as yf
        info = _retry(lambda: yf.Ticker(code_u).info, max_retries=2, label="yf.info")
        if info and (info.get("shortName") or info.get("longName")):
            keep_keys = [
                ("名称",      info.get("shortName") or info.get("longName")),
                ("代码",      code_u),
                ("行业",      info.get("industry") or info.get("sector")),
                ("市值",      info.get("marketCap")),
                ("市盈率TTM", info.get("trailingPE")),
                ("市净率",    info.get("priceToBook")),
                ("最新价",    info.get("currentPrice") or info.get("regularMarketPrice")),
                ("52周高",    info.get("fiftyTwoWeekHigh")),
                ("52周低",    info.get("fiftyTwoWeekLow")),
                ("交易所",    info.get("exchange")),
                ("简介",      (info.get("longBusinessSummary") or "")[:500]),
            ]
            data = [(k, v) for k, v in keep_keys if v not in (None, "", 0)]
            return pd.DataFrame(data, columns=["item", "value"])
    except Exception as e:
        logger.warning(f"yfinance info 失败 {code}: {e}")

    # P1: 雪球（akshare 中转）
    try:
        with _without_system_proxy():
            df = ak.stock_individual_basic_info_us_xq(symbol=code_u)
        if df is not None and not df.empty:
            return df
    except Exception as e:
        logger.warning(f"xueqiu US info 失败 {code}: {e}")

    # P2: itick（付费源，需要 STOCK_ANALYST_ITICK_TOKEN）
    df = _fetch_info_us_itick(code_u)
    if df is not None and not df.empty:
        return df
    return None


# ---------------- K 线 ----------------

def fetch_kline(kind: str, code: str, market: str, target_date: str,
                lookback: int = 120) -> Optional[pd.DataFrame]:
    """target_date: YYYYMMDD。返回标准化 K 线（按日期升序）。"""
    end = datetime.strptime(target_date, "%Y%m%d") + timedelta(days=2)
    start = end - timedelta(days=lookback * 2)
    s_str, e_str = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    s_dash, e_dash = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    try:
        if kind == "astock":
            with _without_system_proxy():
                df = ak.stock_zh_a_daily(symbol=f"{market}{code}",
                                         start_date=s_str, end_date=e_str, adjust="qfq")
        elif kind == "hk":
            with _without_system_proxy():
                df = ak.stock_hk_daily(symbol=code, adjust="qfq")
            df = df[(df["date"] >= start.date()) & (df["date"] <= end.date())]
        elif kind == "etf":
            with _without_system_proxy():
                df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                         start_date=s_str, end_date=e_str, adjust="qfq")
        elif kind == "us":
            return _fetch_kline_us(code, s_str, e_str, s_dash, e_dash)
        else:
            return None
    except Exception as e:
        print(f"  [K 线失败] {type(e).__name__}: {str(e)[:100]}")
        return None

    if df is None or df.empty:
        return None
    return normalize_kline(df)


def _fetch_kline_us(code: str, s_str: str, e_str: str,
                    s_dash: str, e_dash: str) -> Optional[pd.DataFrame]:
    """美股 K 线：先试东财（不走代理），失败再降级到 yfinance（走代理）。"""
    code_u = code.upper()
    # P0: 东财（akshare 中转）
    with _without_system_proxy():
        for sym in _us_secid_candidates(code_u):
            try:
                df = _retry(
                    lambda s=sym: ak.stock_us_hist(
                        symbol=s, period="daily",
                        start_date=s_str, end_date=e_str, adjust="qfq",
                    ),
                    max_retries=1, label=f"ak.stock_us_hist[{sym}]",
                )
                if df is not None and not df.empty:
                    print(f"  ✓ 东财 stock_us_hist (secid {sym})")
                    return normalize_kline(df)
            except Exception as e:
                logger.debug(f"akshare US {sym} 失败: {e}")

    # P1: yfinance
    try:
        _apply_proxy_for_yfinance()
        import yfinance as yf
        df = _retry(
            lambda: yf.Ticker(code_u).history(start=s_dash, end=e_dash),
            max_retries=3, label="yf.history",
        )
        if df is not None and not df.empty:
            df = df.reset_index()
            print(f"  ✓ yfinance.Ticker({code_u}).history")
            return normalize_kline(df)
    except Exception as e:
        print(f"  [yfinance K 线失败] {type(e).__name__}: {str(e)[:100]}")

    # P2: itick（付费源兜底，需要 STOCK_ANALYST_ITICK_TOKEN）
    df = _fetch_kline_us_itick(code_u, s_dash, e_dash)
    if df is not None and not df.empty:
        return df
    return None


# ---------------- 新闻 ----------------

def fetch_news(kind: str, code: str, name: str = "") -> Optional[pd.DataFrame]:
    """ETF 没有公司新闻；A 股/港股用东财；美股用 yfinance Ticker.news。

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
            # A 股 + 港股
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
    """LLM 新闻兜底：调 Claude 列近期新闻，转成与东财兼容的列名。

    依赖 anthropic SDK + ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL（沿用 sentiment_llm 的客户端逻辑）。
    """
    try:
        import anthropic
        import httpx
    except ImportError:
        logger.warning("anthropic SDK 未安装，跳过 LLM 新闻兜底")
        return None

    label = name or code
    prompt = (_LLM_NEWS_PROMPT_EN if market == "us" else _LLM_NEWS_PROMPT_CN).format(
        name=label, code=code, limit=limit,
    )

    explicit_proxy = os.getenv("ANTHROPIC_HTTP_PROXY", "").strip()
    timeout = httpx.Timeout(120.0, connect=10.0)
    if explicit_proxy:
        http_client = httpx.Client(proxy=explicit_proxy, trust_env=False, timeout=timeout)
    else:
        http_client = httpx.Client(trust_env=False, timeout=timeout)

    model = os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-4-7")

    try:
        client = anthropic.Anthropic(http_client=http_client)
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"LLM 新闻兜底调用失败: {e}")
        return None

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    raw = "\n".join(text_parts).strip()
    raw = re.sub(r"```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```", "", raw).strip()

    import json as _json
    m = re.search(r"\[\s*\{[\s\S]*\}\s*\]", raw)
    json_str = m.group(0) if m else raw
    try:
        items = _json.loads(json_str)
    except _json.JSONDecodeError:
        logger.warning(f"LLM 新闻 JSON 解析失败: {raw[:200]}")
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
    print(f"  ✓ LLM 新闻兜底 {len(rows)} 条（model={model}）")
    return pd.DataFrame(rows)


def _fetch_news_us(code: str, limit: int = 30) -> Optional[pd.DataFrame]:
    """yfinance.Ticker.news → 转成与东财格式兼容的 DataFrame（列名：标题/发布时间/文章来源/内容）。

    sentiment_llm 通过子串 '标题'/'内容'/'时间' 匹配列名，所以保持中文列名即可，
    内容本身可以是英文，Claude 双语都能处理。
    """
    _apply_proxy_for_yfinance()
    import yfinance as yf
    raw = _retry(lambda: yf.Ticker(code.upper()).news, max_retries=2, label="yf.news")
    if not raw:
        return None
    rows = []
    for item in raw[:limit]:
        # yfinance >= 0.2.55 把内容包在 content 字段里
        content = item.get("content") or item
        pub_date = content.get("pubDate") or content.get("providerPublishTime", 0)
        if isinstance(pub_date, (int, float)):
            ts = datetime.fromtimestamp(int(pub_date)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            ts = str(pub_date)[:19] if pub_date else ""
        title = content.get("title") or item.get("title", "")
        provider = (content.get("provider") or {}).get("displayName") or item.get("publisher", "")
        summary = content.get("summary") or content.get("description", "")
        rows.append({
            "标题": title,
            "发布时间": ts,
            "文章来源": provider,
            "内容": summary,
        })
    if not rows:
        return None
    return pd.DataFrame(rows)


# ---------------- 资金流（仅 A 股 / ETF） ----------------

def fetch_etf_fund_flow(code: str, target_dash: str) -> Optional[float]:
    """从 fund_etf_spot_em 拿当日 ETF 主力净流入（元）。注意只有当下快照。"""
    try:
        with _without_system_proxy():
            spot = ak.fund_etf_spot_em()
    except Exception:
        return None
    sub = spot[spot["代码"].astype(str) == code]
    if sub.empty:
        return None
    row = sub.iloc[0]
    dd = pd.to_datetime(row.get("数据日期")).strftime("%Y-%m-%d")
    if dd != target_dash:
        return None
    return float(row.get("主力净流入-净额", 0))


def fetch_southbound_summary() -> Optional[pd.DataFrame]:
    """南向资金当日净买入 — 港股市场情绪整体参考。"""
    try:
        with _without_system_proxy():
            return ak.stock_hsgt_hist_em(symbol="南向资金")
    except Exception:
        return None
