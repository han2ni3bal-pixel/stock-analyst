---
name: stock-analyst
description: A 股 / 港股 / ETF / 美股 综合分析与短期走势预测。用户提到任何股票代码（A 股 6 位 / 港股 5 位 / ETF 6 位 / 美股字母代码如 AAPL / TSLA / NVDA）或股票/ETF 名（如"瑞芯微""小米集团""沪深300ETF""苹果""特斯拉"）+ 想看走势/预测/资金流/技术分析/新闻情感/龙虎榜/财报观察 时，必须使用此 skill。也适用于"分析一下 XXX""XXX 怎么看""XXX 下周开盘怎么走""XXX 5/22 涨跌情况""特斯拉财报后会怎么走" 这类问法。即使用户没明确说"用 skill"，只要话题是单只 A 股/港股/ETF/美股 的近期表现或预测，都应当触发。
---

# Stock Analyst — A 股 / 港股 / ETF / 美股 综合分析

## 这个 skill 做什么

输入：代码 + 市场标签（`sh|sz|hk|etf|us`）+ 目标交易日。
输出按品类不同：

| 维度 | A 股 | 港股 | ETF | 美股 |
|---|---|---|---|---|
| 基本信息 | 雪球 | 雪球(HK) | 东财 ETF spot | yfinance.info → 雪球 US → **itick** |
| 日 K 线 | 新浪 | 雪球 | 东财 ETF hist | 东财 stock_us_hist → yfinance → **itick** |
| 价格 + 涨跌 | ✓ | ✓ | ✓ | ✓ |
| 技术指标 (MA/RSI/BOLL/MACD/量比) | ✓ | ✓ | ✓ | ✓ |
| 主力资金流 | 三源回退（push2his/同花顺/雪球） | ✗ 无个股口径 | 东财 ETF spot 快照 | ✗ 无个股口径 |
| 龙虎榜 | ✓ | ✗ | ✗ | ✗ |
| 新闻 + LLM 情感分析 | 东财 → **LLM 兜底** | 东财（含港股新闻）→ **LLM 兜底** | ✗ ETF 无个股新闻 | yfinance.Ticker.news → **LLM 兜底**（英文新闻 Claude 双语处理） |
| **期权流 (PCR/IV)** | ✗ 个股无期权 | ✗ 覆盖不足 | **✓ 期权 ETF**（沪深交所日报 + QVIX） | **✓** yfinance 期权链 |
| **多因子打分卡** | **✓ 全 4 类** | △ 仅技术 + 相对强度 | △ 仅技术 | △ 仅技术 + 相对强度 |

最终给加权信号合成 → 下一交易日开盘倾向（看多/偏多/震荡/偏空/看空 + 置信度）+ **JSON 报告 + PDF 研报（含雷达图）**。

### 多因子打分卡（v2 新增）

4 大类因子，每个因子独立打分 [-1, +1]，类别得分 = 类别内因子均分；类别间按权重合成：

| 类别 | 权重 | A 股因子内容 | 数据源 |
|---|---|---|---|
| **技术因子** | 35% | 5 日动量 / 20 日动量 / 20 日波动率分位 / 量比 / 布林位置 | K 线（已有） |
| **风格因子** | 20% | PE(TTM) 3 年分位 / PB 3 年分位 / PEG | `ak.stock_value_em` |
| **基本面因子** | 25% | ROE / 销售毛利率 / 资产负债率 / 净利润同比 / 营收同比 | `ak.stock_financial_analysis_indicator` |
| **相对强度** | 20% | 个股 vs 行业当日 RS / 个股 vs 上证 20 日 RS | peers 数据 + `ak.stock_zh_index_daily` |

合成后乘 0.8 进总信号 aggregator（避免与已有技术 / 当日涨跌信号过度叠加）。

PDF 报告中：因子卡章节包含 4 维**雷达图**（matplotlib）+ 类别概览表 + **因子有效性检验表** + 各因子明细表。

### 因子有效性检验（v3 新增）

**问题**：单股的因子并非都有预测力。需要识别和剔除噪声因子。

**方法**：单股时序 IC + 滚动 IR + 评级筛选。

| 指标 | 计算 | 阈值 |
|---|---|---|
| **IC** | 因子原始序列 vs 5 日远期收益的 Spearman 秩相关 | \|IC\| ≥ 0.06 |
| **IR** | 60 日滚动 IC 的 mean/std | \|IR\| ≥ 0.5 |
| **样本量** | 至少 100 个有效观测 | n ≥ 100（需 K 线 ≥ 200 天） |

**评级与处置**：
- **A 级**（\|IC\|≥0.10 + \|IR\|≥1.0）：精品因子，全权计入
- **B 级**（\|IC\|≥0.06 + \|IR\|≥0.5）：通过，全权计入
- **C 级**（仅一项通过）：弱有效，**半权重**
- **D 级**（都不达标）：无效因子，**剔除**（不参与打分）

**可验证因子**（共 8 项，时序可计算）：
- 技术：5d 动量、20d 动量、20d 波动率、量比、布林位置
- 风格：PE(TTM) 历史分位、PB 历史分位
- 相对强度：vs 上证 20 日 RS

**静态因子**（财务 5 项 + PEG + vs 行业当日 RS = 7 项）：单点取值，样本量不足做时序 IC，按原权重保留。

**实际效果**（实测 603893 瑞芯微）：
- 关闭检验时：综合信号 +2.28 → 看多 ↑↑（置信度中高）
- 开启检验后：5d 动量被评为 D 级剔除，20d 波动率剔除，剩余 3 个 C 级降半权重 → 综合信号 **+0.38 弱偏多（置信度低）**
- 实际 5/28 大跌 -3.92%——**有效性检验把"过度自信看多"修正为"低置信度震荡"，预测更稳健**

### 期权流 PCR / 隐含波动率（v4 新增）

看市场用期权在押注什么方向，作为偏情绪/资金面的二级信号（内部 cap ±0.6）。脚本 `scripts/options_flow.py`。

| 市场 | 数据源 | 历史可回溯 | 覆盖标的 |
|---|---|---|---|
| A 股 ETF/指数期权 | `ak.option_daily_stats_sse / _szse(date=YYYYMMDD)` + QVIX(`index_option_*_qvix`) | **✓ 按 target_date 精确对齐** | 510050/510300/510500/588000/588080、159901/159915/159919/159922 |
| 美股 | `yfinance.Ticker(code).option_chain` | ✗ 仅当前快照（与 target_date 相差 >5 天会标注） | 大多数有期权的标的 |

核心指标与打分：
- **PCR（成交量）** = 认沽/认购成交比。<0.85 看涨持仓占优(+)，>1.15 认沽活跃/对冲偏空(-)，极端值逆向减弱。
- **PCR（未平仓）**：存量仓位倾向，同向时加强 ±0.15。
- **隐含波动率 IV**（A 股取 QVIX，美股取近月期权链 IV 中位数）：近 5 日相对抬升 >15% 减 0.1（避险升温），回落 >15% 加 0.1。
- A 股个股 / 港股本身无可交易期权 → `available=False`，0 分计入并在报告中标注"跳过"。
- 美股期权链带 1 小时文件缓存（`$TMPDIR/stock_analyst_opt_cache`），缓解 yfinance 限速。

`analyze.py` 加 `--no-validate-factors` 可跳过检验（debug 用）。

## 如何运行

> **路径占位符**：以下命令中 `<SKILL_DIR>` 指代 skill 安装路径，通常是 `~/.claude/skills/stock-analyst`。Claude 每次激活 skill 时会得到一个 "Base directory" 提示，用那个路径替换 `<SKILL_DIR>` 即可。

```bash
# Python 选择规则：默认 python3；用户若设了 STOCK_ANALYST_PYTHON 优先用它
PY="${STOCK_ANALYST_PYTHON:-python3}"

# A 股
$PY <SKILL_DIR>/scripts/analyze.py 603893 sh 20260522 瑞芯微 \
  --out <SKILL_DIR>/output

# 港股（5 位代码）
$PY <SKILL_DIR>/scripts/analyze.py 01810 hk 20260522 小米集团 --out <SKILL_DIR>/output

# ETF（6 位代码，市场写 etf 即可，脚本自动判断沪/深）
$PY <SKILL_DIR>/scripts/analyze.py 510300 etf 20260522 沪深300ETF --out <SKILL_DIR>/output

# 美股（字母代码，全大写）
$PY <SKILL_DIR>/scripts/analyze.py AAPL us 20260522 Apple --out <SKILL_DIR>/output
$PY <SKILL_DIR>/scripts/analyze.py TSLA us 20260522 Tesla --out <SKILL_DIR>/output
```

参数：
- `code`：A 股 6 位 / 港股 5 位（带前导 0）/ ETF 6 位 / 美股字母代码（全大写，如 `AAPL` / `TSLA` / `BRK.B`）
- `market`：`sh` / `sz` / `hk` / `etf` / `us`
- `target_date`：YYYYMMDD，**必须是已收盘的交易日**
- `name`（可选）：仅用于日志和情感 prompt 上下文
- `--out`：输出目录，缺省 `./output`

### 自动判断市场前缀（A 股）

| 代码前缀 | 市场 | 说明 |
|---|---|---|
| 600/601/603/605/688 | `sh` | 沪市主板 + 科创板 |
| 000/002 | `sz` | 深主板 + 中小板 |
| 300/301 | `sz` | 创业板 |
| 4/8 开头 | 北交所 | **不支持**，告诉用户 |

### 港股代码

A 股给 6 位（如 `603893`），港股给 5 位（如 `01810`）。如果用户给的代码是 5 位且带前导 0，那一定是港股。
注意港股有些热门票只用 4 位说，例如"小米 1810" → 跑的时候补 0 成 `01810`。

### ETF 识别

ETF 是 6 位代码，常见前缀：
- `5xxxxx`、`56xxxx`、`58xxxx` → 沪市 ETF
- `15xxxx`、`16xxxx`、`159xxx` → 深市 ETF

ETF 例子：`510300` 沪深300ETF / `510500` 中证500ETF / `159915` 创业板ETF / `512100` 中证1000ETF。

如果用户问"沪深300ETF""中概互联ETF"等名称但没给代码，可以让 Claude 直接用知识回忆代码，或用 `ak.fund_etf_spot_em()` 模糊查询（按"名称"列匹配关键词）。

### 美股识别

美股是字母代码（1-5 个大写字母，可能含 `.`）。用户给代码时直接转大写传 `us`：
- `AAPL` Apple / `MSFT` Microsoft / `NVDA` Nvidia / `TSLA` Tesla / `META` Meta / `AMZN` Amazon
- `GOOGL` Alphabet / `BRK.B` Berkshire B 类 / `BABA` 阿里 ADR / `JD` 京东 ADR

如果用户给中文名（"特斯拉""英伟达""苹果""阿里""谷歌"等），让 Claude 直接对应到 ticker（这些是常识级映射）。

> ⚠️ **网络要求**：美股数据走 Yahoo Finance（yfinance）和东财海外口径，**通常需要代理才能稳定访问**。
>
> 设置代理两种方式：
> 1. 环境变量 `STOCK_ANALYST_PROXY=http://127.0.0.1:7890`（专给 yfinance 用，不会污染其他国内接口）
> 2. macOS 系统代理（系统设置里开启 HTTP/HTTPS 代理），脚本自动通过 `scutil --proxy` 探测并应用到 yfinance（仅 macOS）
>
> 如果两个源都失败：先 `scutil --proxy` 看代理是否开启；没开就让用户开代理或设环境变量再重跑。
>
> **itick 兜底（推荐配付费 token）**：当 yfinance 限速或东财 secid 不命中时，脚本会再走一次 itick 直连 API（`api0.itick.org`，不走代理）。设置 `STOCK_ANALYST_ITICK_TOKEN=<your-token>` 即可启用，未配置时自动跳过。token 可在 [docs.itick.org](https://docs.itick.org) 申请。
>
> **LLM 新闻兜底**：当 yfinance.news / 东财新闻接口取空时，脚本会调一次 Claude（沿用 sentiment_llm 的 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 配置）让模型列出近期新闻 JSON。**前提是 LLM 端能联网**或训练数据足够新；如果模型只会幻觉，关掉这条兜底（暂未提供开关，必要时可在 `data_layer.py` 注释 `_fetch_news_via_llm` 调用）。

## 何时该用此 skill

只要用户的问题落在以下任一桶里，立刻调用：
- "分析一下 XXX 这只股票/ETF/港股"
- "XXX 下周开盘会涨吗 / 怎么走"
- "XXX 在 X 月 X 日的资金流向 / 涨跌情况"
- "XXX 最近的新闻面 / 情感分析"
- "XXX 的技术指标看 / RSI/MACD/均线"
- "XXX 上没上龙虎榜"
- "特斯拉/英伟达/苹果 财报后会怎么走" / "AAPL 下个交易日开盘怎么看"
- 给出代码（6 位 A 股 / 5 位港股 / 6 位 ETF / 字母 美股）+ 任何分析意图

如果用户只问基本面（市值/PE/营收）而没有"分析/预测/最近怎么样"等意图，可以直接用基本信息接口查一下，不必跑全套流程。

## 使用流程

### Step 1：补全参数

如果用户没说全（比如只给了名字"瑞芯微"或者只给了代码），按下面顺序补：

1. **代码**：用户给名字的话，可以问用户或用 `ak.stock_info_a_code_name()`（A 股）、`ak.fund_etf_spot_em()`（ETF）查；港股没有方便的全量接口，可以让 Claude 直接回忆
2. **市场**：按代码长度+前缀规则推断（参见上文），不用问用户
3. **目标日期**：默认上一个交易日（参考当前对话日期，跳过周末/节假日）

如果信息明确，直接跑，不要反复确认。

### Step 2：运行脚本

用 Bash 工具调用 `analyze.py`。**Python 解释器选择规则**：

```bash
# 优先用户显式指定的解释器
PY="${STOCK_ANALYST_PYTHON:-python3}"
```

如果用户的环境里 `python3` 没装齐依赖（akshare、anthropic 等），跑脚本会 ImportError，这时建议用户：

```bash
pip install -r <SKILL_DIR>/requirements.txt
# 或者，如果他们用 venv / pyenv / conda，告诉他们：
export STOCK_ANALYST_PYTHON=/path/to/their/python
```

> **首次安装应先跑自检**：`$PY <SKILL_DIR>/scripts/check_env.py` — 验证 Python、依赖、Chrome、API 连通性。

### Step 3：读结果

脚本会打印分章节的中间数据 + 最终的"信号合计 + 走势倾向"。
**每次运行同时输出两份报告**到 `--out/`：
- `report_{code}_{date}.json` — 结构化原始数据（程序可读）
- `report_{code}_{date}.pdf` — 中文研报样式 PDF（A4，含基本信息表、近 6 日 K 线表、技术快照、新闻事件、**多因子打分卡 + 雷达图**、板块联动、信号汇总，并由 Claude 自动追加一段"综合解读"段落）

PDF 渲染依赖：`markdown` Python 包 + `matplotlib`（雷达图）+ Chrome / Chromium / Edge headless（自动跨平台探测，可用 `CHROME_BIN` 覆盖）。Chrome 不存在时降级仅写 HTML；matplotlib 不存在时跳过雷达图但保留因子表；Claude 综合解读失败时静默跳过，不影响 PDF 主体。

把关键结论用中文总结给用户，重点突出：
- 当日涨跌幅 + 资金流方向
- 技术形态特征（多头/空头/超买超卖）
- **因子打分**（4 类得分概览 + 加权分；指出最强项与最弱项）
- LLM 情感判断的核心理由（如果有）
- 总分 + 走势倾向 + 置信度
- 置信度低或信号矛盾时，明确告诉用户
- **PDF 路径**（用户可能想直接打开或转发）

**对 ETF**：没有新闻情感、龙虎榜和基本面/风格因子，所以信号主要来自当日涨跌 + 技术指标 + 主力资金 + 技术因子。如果 fund_etf_spot_em 快照不在目标日（脚本是当下时刻拉的），资金流会显示"快照不在目标日"，这时建议改用最近的交易日重跑。

**对港股**：没有个股资金流和龙虎榜，因子卡仅有技术 + 相对强度类（基本面接口异源未实现），主要信号来自技术指标 + 当日涨跌 + 新闻情感。

**对美股**：没有个股资金流和龙虎榜，因子卡仅有技术 + 相对强度类（基本面用 yfinance 聚合的 Yahoo Finance 新闻代替），主要信号来自技术指标 + 当日涨跌 + 新闻情感（多为英文，Claude 直接读英文判断方向）。需要代理访问 Yahoo Finance；东财海外口径不稳，常需 yfinance 兜底。

## 数据源/调用细节

详见：
- [data_sources.md](references/data_sources.md) — 各维度数据源优先级、调用方式、风控判断
- [signal_logic.md](references/signal_logic.md) — 信号评分规则、聚合权重、走势映射阈值
- [info_sources.md](references/info_sources.md) — **信息储备层(公告/年报/季报/IPO…)** 源清单与 EDGAR/代理细节

> **信息储备层(P1+P2,已接入主流程)**:`scripts/info_store.py`(SQLite)+ `info_adapters.py`
> (A股公告=东财、美股 filing=SEC EDGAR)+ `info_enrich.py`(LLM 加工)+ `info_signal.py`(事件面因子)。
> 把公告/年报/季报/IPO 归一成「事件卡」落盘(增量同步、主键去重、`event_date<=target` 防前视),
> 再由 LLM 标注 `sentiment(-1..1)/materiality(0..3)` → 事件面信号(cap ±0.6)进总信号 + PDF「十、信息面」章节。
> `analyze.py` 会自动跑(§10 信息面);ETF/港股暂不覆盖。独立验收 CLI:
> `python scripts/probe_info.py 603893 sh 20260608 瑞芯微` / `… AAPL us 20260608 Apple`。
> **P3 待做**:全文 PDF 下载解析、A股调研/投资者问答/研报、美股 yfinance 财报日历。

## 信号合成顺序（v5 含期权流 + 信息面）

```
当日涨跌 + 技术指标(×0.7) + 资金流 + 龙虎榜 + LLM 情感(×置信度) + 多因子合成(weighted ×0.8) + 期权流(PCR/IV, cap ±0.6) + 信息面(cap ±0.6)
                                                                  └─ 35% 技术 + 20% 风格 + 25% 基本面 + 20% 相对强度
                                                                     ↓ 每个时序因子先做 IC/IR 检验
                                                                     ↓ A/B 级全权 ｜ C 级半权 ｜ D 级剔除
                                                                     ↓ 静态因子原权重保留
                                                                     类别内按通过因子均分聚合
↓
信号合计 → 走势倾向（看多/偏多/弱偏多/震荡/弱偏空/偏空/看空）+ 置信度

信息面 = Σ[ sentiment × (materiality/3) × exp(-days_ago/30) ]，clip 到 ±0.6
         事件卡由信息储备层(公告/年报/季报/事件)经 LLM 标注 sentiment/materiality 得到
```

## 故障排查

### 首次安装报错 "ModuleNotFoundError"

跑 `pip install -r <SKILL_DIR>/requirements.txt`，再 `python <SKILL_DIR>/scripts/check_env.py` 验证。
如果用户有特定 Python 环境（pyenv/conda/venv），让他们 `export STOCK_ANALYST_PYTHON=/path/to/python` 然后再调用 skill。

### `push2.eastmoney.com` 风控（IP 被临时拉黑）

症状：A 股资金流 push2his 失败 / ETF K 线 fund_etf_hist_em 失败，报 `RemoteDisconnected`。

排查：跑 `python scripts/probe_data_sources.py` 看哪些源还能用。

应对：
1. A 股资金流已经默认绕过 push2 主域，会自动 fallback 到同花顺/雪球
2. 临时换网络（家宽切热点、VPN 切节点）通常几小时-几天恢复
3. 设置代理：`export HTTPS_PROXY=http://...` 后再跑

### LLM 情感分析失败

如果输出 `LLM情感: 分析失败: ...`：
- `anthropic SDK 未安装`：`pip install anthropic`
- `模型输出无法解析为 JSON`：偶发，重跑一次通常 OK
- `Connection error / Server disconnected`：检查 `ANTHROPIC_BASE_URL` 是否可达；如果是内网网关而本机有系统代理，脚本默认 `trust_env=False` 已绕过，但极端情况可显式 `export ANTHROPIC_HTTP_PROXY=http://...`

### PDF 没生成（只有 HTML）

Chrome 没装或路径不对。装 Chrome / Chromium / Edge 任一，或 `export CHROME_BIN=/path/to/chrome`。
跑 `python scripts/check_env.py` 看是否能正确探测到。

### 目标日无 K 线

最常见原因：用户给的日期是周末/节假日/停牌日。脚本会打印"目标日无 K 线"。
处理：自动回退到前一个交易日，或问用户确认目标日。

### 北交所代码（4/8 开头）

不支持。告诉用户：本 skill 仅覆盖沪深 A 股/港股主流票/ETF/美股，北交所数据接口不同，暂未实现。

### 港股代码格式

港股要 5 位（带前导 0）：小米 → `01810`，腾讯 → `00700`，比亚迪 → `01211`。
用户如果只说 4 位（比如"1810"），自动补一个前导 0。

### 美股 Yahoo Finance 限速 / 失败

症状：
- `Too Many Requests. Rate limited. Try after a while.` ← yfinance 被 Yahoo 限速
- `RemoteDisconnected` 走 `stock_us_hist` 时 ← 东财 push2 被风控

排查 / 应对：
1. 优先打开代理（`STOCK_ANALYST_PROXY` 或 macOS 系统代理）— 美股**几乎必须走代理**才能稳；
2. 同一 ticker 短时间内连续重试会被持续封禁，建议至少等 5-10 分钟；
3. 如果 yfinance 返回 `'data'` KeyError 或空响应，多是雪球 token 失效或 ticker 不在库里，换 `STOCK_ANALYST_PROXY` 节点重跑通常 OK；
4. **配 itick token**（`STOCK_ANALYST_ITICK_TOKEN`）作为第三层兜底，付费源稳定且不依赖代理。

### itick 报错

- `STOCK_ANALYST_ITICK_TOKEN 未配置` — 没设环境变量，跳过 itick 兜底（不影响其他源）
- `itick code=N msg=...` — 业务错（限频 / token 过期 / region 错），看 msg；常见 N=10410（无权限）需检查套餐
- `itick HTTP 4xx/5xx` — 网络或服务端故障，重试或切代理（默认 itick 不走代理，可临时 `unset trust_env` 调试）

### LLM 新闻兜底没有结果

- `anthropic SDK 未安装` — `pip install anthropic`
- `LLM 新闻 JSON 解析失败` — 模型偶发返回非 JSON，重跑通常 OK
- 模型联网能力弱时返回的可能全是过期新闻，建议把这类输出当作"补充上下文"而非"最新事件"，必要时关闭兜底

## 局限

- 仅作流程演示，**不构成投资建议**
- 技术指标对 1-3 天的短期预测可靠性有限
- 情感分析依赖 LLM 判断，对小道消息/谣言识别不稳定
- 港股没有个股资金流口径（只有市场南向资金，太粗）
- ETF 无新闻情感，信号偏少；快照接口只能给"当下"的资金流，T+0 后查 T 日的会失效
- 美股没有个股资金流口径（更粗的机构持仓 13F 季报不适合短期判断），也没有龙虎榜对应口径
- 未考虑大盘 beta / 行业政策 / 板块轮动 / 衍生品持仓 / 公司治理

每次给用户结果时附上一句免责声明。
