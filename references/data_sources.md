# 数据源备忘

每个分析维度的可用数据源、调用方式、风控/失效特征。优先用第一项；前一项失败再降级。

## 1. 个股基本信息

| 优先级 | 来源 | 调用 | 备注 |
|---|---|---|---|
| P0 | 雪球 | `ak.stock_individual_basic_info_xq(symbol="SH603893")` | 稳定；返回 39 行 (item, value) |
| P1 | 东财 | `ak.stock_individual_info_em(symbol="603893")` | 走 push2，**易被拉黑** |

## 2. 日 K 线

| 优先级 | 来源 | 调用 | 备注 |
|---|---|---|---|
| P0 | 新浪 | `ak.stock_zh_a_daily(symbol="sh603893", start_date, end_date, adjust="qfq")` | symbol 用 `sh/sz+code` 小写 |
| P1 | 东财 | `ak.stock_zh_a_hist(symbol="603893", period="daily", ...)` | 走 push2，常 fail |

注意：算 MA60 至少需要 60 个交易日，建议 lookback 取 120 自然日（即 ~80 交易日）。

## 3. 资金流向

| 优先级 | 来源 | 调用 | 单位 | 备注 |
|---|---|---|---|---|
| P0 | 东财 push2his | `from_eastmoney_push2his(code, market_id, days=20)` | 元 | 与 push2 不同子域，常仍可用 |
| P1 | 同花顺 | `ak.stock_fund_flow_individual(symbol="即时")` 后 filter | 亿 | 仅当日即时数据；想要历史用 push2his |
| P2 | 雪球 | `from_xueqiu(code, "SH"/"SZ", count)` | 元 | **分钟级**，需自动获取 cookie；要按日聚合 |
| P3 | 东财 datacenter | `RPT_VALUEANALYSIS_DET` | — | 实际是估值数据，不要当资金流用 |

⚠️  `ak.stock_individual_fund_flow(stock, market)` 走 push2 主域，**已被风控**，不要用。

## 4. 新闻

| 优先级 | 来源 | 调用 | 备注 |
|---|---|---|---|
| P0 | 东财 datacenter | `ak.stock_news_em(symbol="603893")` | 稳；约 10 条最近新闻 |

## 5. 龙虎榜

| 优先级 | 来源 | 调用 | 备注 |
|---|---|---|---|
| P0 | 东财 datacenter | `ak.stock_lhb_detail_em(start_date, end_date)` | 区间内全部上榜股 |

## 风控诊断

`push2.eastmoney.com` 被拉黑的特征：
```
* Connected to push2.eastmoney.com:443
* SSL handshake completes ✓
> GET /api/qt/clist/get HTTP/1.1
* Empty reply from server          ← 服务器收到请求后直接断开
```
TLS 通了但 0 字节响应 = IP 级反代 reset。
应对：
1. 换网络 / 换出口 IP（家宽切换、热点、VPN 节点）— 通常几小时到几天恢复；
2. `os.environ['HTTPS_PROXY'] = 'http://...'`
3. 不走 push2 的接口（新浪 / 雪球 / 同花顺 / datacenter）。

跑 `scripts/probe_data_sources.py` 可以快速判断当前网络下哪些源能用。
