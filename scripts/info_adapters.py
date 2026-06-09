"""信息储备层 — 采集适配器 (P1)。

按市场分适配器,但都归一到同一张事件卡 (info_store.EventCard),下游不区分市场。

P1 实现两个:
- AShareAnnouncementAdapter : A 股公告。东财 ak.stock_individual_notice_report,自带「公告类型」+ 链接。
- USFilingAdapter           : 美股 filing。SEC EDGAR(ticker→CIK 缓存 → submissions),免 key 免代理。

每个 adapter.fetch(code, market, name, since_date) 返回「归一后的 EventCard 列表」(只含
event_date > since_date 的增量),交给 info_store.upsert_events 落库(自动去重)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional

from info_store import STORE_DIR, EventCard

logger = logging.getLogger(__name__)


def _detect_macos_proxies() -> list[str]:
    """读取 macOS 系统代理,返回 requests 风格 proxy URL 列表(SOCKS 优先,再 HTTP)。

    很多代理工具(Clash/Surge 等)只起 SOCKS5,并把 HTTP 代理端口填成 65535 占位 ——
    所以这里既认 SOCKS 也认 HTTP,且跳过 65535 这种无效端口。
    """
    out_list: list[str] = []
    try:
        out = subprocess.check_output(["scutil", "--proxy"], text=True, timeout=3)

        def grab(prefix: str, scheme: str):
            if f"{prefix}Enable : 1" not in out:
                return
            host = re.search(rf"{prefix}Proxy : (\S+)", out)
            port = re.search(rf"{prefix}Port : (\d+)", out)
            if host and port and port.group(1) != "65535":
                out_list.append(f"{scheme}://{host.group(1)}:{port.group(1)}")

        grab("SOCKS", "socks5h")
        grab("HTTPS", "http")
        grab("HTTP", "http")
    except Exception:
        pass
    return out_list

_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
)


@contextmanager
def _without_system_proxy():
    """临时清进程级代理,避免国内 akshare 接口被 macOS 系统代理误伤。"""
    saved = {k: os.environ[k] for k in _PROXY_ENV_KEYS if k in os.environ}
    for k in _PROXY_ENV_KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k in _PROXY_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(saved)


# ---------------- 类型归一(标准化的关键,新增类型只改这里) ----------------

_US_FORM_TYPE = {
    "10-K": "年报", "10-K/A": "年报", "20-F": "年报", "40-F": "年报",
    "10-Q": "季报", "10-Q/A": "季报",
    "8-K": "临时公告", "8-K/A": "临时公告", "6-K": "临时公告",
    "S-1": "IPO", "S-1/A": "IPO", "F-1": "IPO",
    "DEF 14A": "股东大会", "DEFA14A": "股东大会",
    "4": "高管交易", "3": "高管交易", "5": "高管交易",
}

_A_TYPE_RULES = [
    ("年报", ("年度报告",)),
    ("季报", ("季度报告", "第一季度", "第三季度", "半年度报告", "半年报")),
    ("IPO", ("首次公开发行", "招股", "上市公告书")),
    ("股东大会", ("股东大会",)),
    ("高管交易", ("增持", "减持", "股份变动", "持股变动")),
]


def normalize_type(market: str, subtype: str) -> str:
    """原始分类串 → 归一 type。美股按 form 字典,A 股按关键词规则。"""
    subtype = (subtype or "").strip()
    if market == "us":
        if subtype.startswith("424B"):
            return "IPO"
        return _US_FORM_TYPE.get(subtype, "其他")
    for t, kws in _A_TYPE_RULES:
        if any(k in subtype for k in kws):
            return t
    return "临时公告" if subtype else "其他"


def _default_since(since_date: str | None, lookback_days: int = 90) -> str:
    """首次同步(无水位)默认回看 lookback_days 天。"""
    if since_date:
        return since_date
    return (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")


# ---------------- A 股公告:东财 ----------------

class AShareAnnouncementAdapter:
    source = "eastmoney"

    def supports(self, market: str) -> bool:
        return market in ("sh", "sz")

    def fetch(self, code: str, market: str, name: str, since_date: str | None) -> list[EventCard]:
        import akshare as ak
        since = _default_since(since_date)
        with _without_system_proxy():
            df = ak.stock_individual_notice_report(security=code)
        cards: list[EventCard] = []
        for _, r in df.iterrows():
            ev_date = str(r.get("公告日期", "")).strip()[:10]
            if not ev_date or ev_date < since:  # >= 水位都复拉,靠主键去重(防同日漏)
                continue
            url = str(r.get("网址", "")).strip()
            subtype = str(r.get("公告类型", "")).strip()
            native = self._native_id(url, ev_date, str(r.get("公告标题", "")))
            cards.append(EventCard(
                event_id=f"{market}:{code}:{self.source}:{native}",
                code=code, market=market, name=name or str(r.get("名称", "")),
                type=normalize_type(market, subtype), subtype=subtype,
                title=str(r.get("公告标题", "")).strip(),
                event_date=ev_date, source=self.source, url=url,
            ))
        logger.info("A股公告 %s: since>%s 命中 %d 条", code, since, len(cards))
        return cards

    @staticmethod
    def _native_id(url: str, ev_date: str, title: str) -> str:
        m = re.search(r"(AN\d+)", url)
        if m:
            return m.group(1)
        return f"{ev_date}-{abs(hash(title)) % 10**10}"


# ---------------- 美股 filing:SEC EDGAR ----------------

class USFilingAdapter:
    source = "sec_edgar"
    _CIK_CACHE = os.path.join(STORE_DIR, "cik_map.json")
    _CIK_TTL = 7 * 86400  # 7 天刷一次

    def supports(self, market: str) -> bool:
        return market == "us"

    @property
    def _ua(self) -> str:
        return os.environ.get("STOCK_ANALYST_SEC_UA", "stock-analyst han2ni3bal@outlook.com")

    def _proxy_attempts(self) -> list[Optional[dict]]:
        # EDGAR 是境外源:多数内地网络需走代理。依次尝试 env 代理 → 系统代理(SOCKS/HTTP)→ 直连,
        # 谁先通用谁。代理工具开着时走代理成功;在能直连的网络(如美国本地)最终回退直连。
        urls = [os.environ.get("STOCK_ANALYST_PROXY", "").strip()] + _detect_macos_proxies()
        attempts: list[Optional[dict]] = [{"http": u, "https": u} for u in urls if u]
        attempts.append(None)  # 直连兜底
        return attempts

    def _get(self, url: str, timeout: int = 20) -> bytes:
        import requests
        last: Exception | None = None
        for proxies in self._proxy_attempts():
            try:
                time.sleep(0.12)  # EDGAR 礼貌间隔(限 10 req/s)
                r = requests.get(url, headers={"User-Agent": self._ua}, proxies=proxies, timeout=timeout)
                r.raise_for_status()
                return r.content
            except Exception as e:
                last = e
        raise last  # type: ignore[misc]

    def _cik_map(self) -> dict:
        fresh = os.path.exists(self._CIK_CACHE) and (time.time() - os.path.getmtime(self._CIK_CACHE)) < self._CIK_TTL
        if fresh:
            with open(self._CIK_CACHE, encoding="utf-8") as f:
                return json.load(f)
        raw = json.loads(self._get("https://www.sec.gov/files/company_tickers.json"))
        m = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in raw.values()}
        os.makedirs(STORE_DIR, exist_ok=True)
        with open(self._CIK_CACHE, "w", encoding="utf-8") as f:
            json.dump(m, f)
        return m

    def fetch(self, code: str, market: str, name: str, since_date: str | None) -> list[EventCard]:
        since = _default_since(since_date)
        cik = self._cik_map().get(code.upper())
        if not cik:
            logger.warning("EDGAR 未找到 ticker=%s 的 CIK", code)
            return []
        j = json.loads(self._get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
        rec = j.get("filings", {}).get("recent", {})
        forms = rec.get("form", [])
        dates = rec.get("filingDate", [])
        accs = rec.get("accessionNumber", [])
        docs = rec.get("primaryDocument", [])
        descs = rec.get("primaryDocDescription", [])
        cik_int = int(cik)
        nm = name or j.get("name", "")
        cards: list[EventCard] = []
        for i, form in enumerate(forms):
            ev_date = (dates[i] if i < len(dates) else "")[:10]
            if not ev_date or ev_date < since:  # >= 水位都复拉,靠主键去重
                continue
            acc = accs[i] if i < len(accs) else ""
            doc = docs[i] if i < len(docs) else ""
            url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-', '')}/{doc}" if acc and doc else ""
            cards.append(EventCard(
                event_id=f"{market}:{code}:{self.source}:{acc or ev_date+'-'+form}",
                code=code, market=market, name=nm,
                type=normalize_type(market, form), subtype=form,
                title=(descs[i] if i < len(descs) else "") or form,
                event_date=ev_date, source=self.source, url=url,
                raw_meta={"cik": cik, "accession": acc, "form": form},
            ))
        logger.info("美股 filing %s: since>%s 命中 %d 条", code, since, len(cards))
        return cards


ADAPTERS = [AShareAnnouncementAdapter(), USFilingAdapter()]


def adapters_for(market: str) -> list:
    return [a for a in ADAPTERS if a.supports(market)]


def sync_events(conn, code: str, market: str, name: str) -> int:
    """对一只股跑所有适配器:读水位 → 增量 fetch → upsert(去重) → 更新水位。返回新增条数。

    market 用数据层口径(sh/sz/us);hk/etf 无适配器时直接返回 0(信息储备层暂不覆盖)。
    """
    import info_store as store
    total_new = 0
    for ad in adapters_for(market):
        since = store.get_sync_state(conn, code, market, ad.source)
        try:
            cards = ad.fetch(code, market, name, since)
        except Exception as e:
            logger.warning("[%s] fetch 失败(非致命): %s: %s", ad.source, type(e).__name__, str(e)[:120])
            continue
        inserted, _ = store.upsert_events(conn, cards)
        total_new += inserted
        max_date = max((c.event_date for c in cards), default=since)
        if max_date:
            store.set_sync_state(conn, code, market, ad.source, max_date)
    return total_new
