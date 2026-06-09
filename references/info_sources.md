# 信息储备层 — 数据源清单

信息面(公告/年报/季报/IPO/调研/问答/研报)的可用源、调用方式、稳定性。
实测日期 2026-06-08。配套设计见桌面 `info_storage_design.md`,代码见 `scripts/info_*.py`。

> **P1+P2 状态(均已验收)**:
> - **P1**:`info_store.py` + `info_adapters.py`(A股公告 + 美股 EDGAR)+ `probe_info.py`。
>   验收 `python scripts/probe_info.py <code> <market> <YYYYMMDD> [name]`:落库/去重/防前视三条全过。
> - **P2**:`info_enrich.py`(LLM 标注 sentiment/materiality,批量单次、幂等)+ `info_signal.py`
>   (事件面 = Σ sentiment×(materiality/3)×exp(-days/30),cap ±0.6)+ 接入 `analyze.py` §10 + PDF「十、信息面」。
>   验收:A股事件面 +0.42 / 美股 -0.33 进信号合计,PDF 出表,幂等复跑加工 0 条。
> - **P3 待做**:全文 PDF 下载解析、A股调研/问答/研报、美股 yfinance 财报日历。

## A 股(样本 603893 瑞芯微)

| 信息类型 | 接口 | 状态 | 字段 | P1 是否启用 |
|---|---|---|---|---|
| **公告(个股)** | `ak.stock_individual_notice_report(security=code)` | ✅ ~1052 条 / 2.8s | 代码/名称/标题/**公告类型**/日期/网址 | **✅ 启用** |
| 公告全文(权威) | `ak.stock_zh_a_disclosure_report_cninfo(symbol,market,start,end)` | ✅ 67 条 / 1.2s | 含巨潮 PDF 链接 | Phase 3 全文 |
| IPO 摘要 | `ak.stock_ipo_summary_cninfo(symbol=code)` | ✅ 1 条 / 0.4s | 发行价/市盈率/募资/上市日…15 列 | Phase 2/3 |
| 投资者问答(深市) | `ak.stock_irm_cninfo(symbol=code)` | ✅ 310 条 / 1.6s | 问题/时间/**回答内容**/回答者 | Phase 3 |
| 机构调研/访谈 | `ak.stock_jgdy_detail_em(date=YYYYMMDD)` | ✅ 816 条 / 5.3s | 按天**全市场**,需 filter code | Phase 3 |
| 卖方研报 | `ak.stock_research_report_em(symbol=code)` | ✅ 80 条 / 0.5s | 含研报 PDF 链接 | Phase 3 |
| 投资者问答(沪市) | `ak.stock_sns_sseinfo(symbol=code)` | ⚠️ **慢且空**(19s/0 条) | — | 不纳入 |

> 公告的 `公告类型` 字段东财已分好类,入库时直接做关键词归一,无需自建 NLP 分类。

## 美股(样本 AAPL)

| 信息类型 | 源 | 状态 | P1 是否启用 |
|---|---|---|---|
| **公告/年报/季报/IPO 全部** | **SEC EDGAR** JSON API | ✅ 直连 1.6s,**免 key 免代理** | **✅ 启用** |
| 财务报表数字 | `ak.stock_financial_us_report_em(stock=code, symbol='资产负债表', indicator='年报')` | ✅ 735 行 / 1.0s | Phase 3 |
| 财报日期/电话会/新闻 | yfinance | ⚠️ 限速,需代理,flaky | Phase 3 |

### EDGAR 调用(两跳)

1. ticker→CIK:`GET https://www.sec.gov/files/company_tickers.json`(全量 ~795KB,缓存 `store/cik_map.json`,7 天刷)
2. 某公司全部 filing:`GET https://data.sec.gov/submissions/CIK{cik10位}.json` → `filings.recent` 并行数组
3. 单 filing 原文:`https://www.sec.gov/Archives/edgar/data/{cik}/{accession去横线}/{primaryDocument}`

**强制带 User-Agent**(否则 403):默认 `stock-analyst han2ni3bal@outlook.com`,可用环境变量 `STOCK_ANALYST_SEC_UA` 覆盖。限速 10 req/s,代码内置 0.12s 间隔。

**代理(境外源,内地多数网络需走代理)**:EDGAR 用 `requests` 发请求,代理按 `STOCK_ANALYST_PROXY` → macOS 系统代理(**SOCKS5 优先,再 HTTP**)→ 直连 依次尝试。注意很多代理工具(Clash/Surge)只起 SOCKS5、把 HTTP 端口填 `65535` 占位 —— 代码会跳过 65535 并优先用 SOCKS5(`socks5h://`,需 `PySocks`)。实测本机走系统 SOCKS5 `127.0.0.1:8119` 通,1s 返回。

### EDGAR form → 归一类型

| form | 归一 type |
|---|---|
| 10-K / 20-F / 40-F | 年报 |
| 10-Q | 季报 |
| 8-K / 6-K | 临时公告(含财报发布) |
| S-1 / F-1 / 424B* | IPO |
| DEF 14A | 股东大会 |
| 3 / 4 / 5 | 高管交易 |
| 其他 | 其他 |

## 拿不到的(边界)

- **美股电话会逐字稿、投资者问答**:无免费稳定源(Seeking Alpha 等多付费/需爬),方案不含。
- **沪市投资者问答**(sseinfo):慢且常空,不纳入。
