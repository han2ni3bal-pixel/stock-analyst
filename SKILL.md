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
| 基本信息 | Tickflow | Tickflow | Tickflow | Tickflow |
| 日 K 线 | Tickflow | Tickflow | Tickflow | Tickflow |
| 实时 / 盘中 | Tickflow | Tickflow | Tickflow | 盘中 Tickflow；盘前/盘后 yfinance prepost |
| 价格 + 涨跌 | ✓ | ✓ | ✓ | ✓ |
| 技术指标 (MA/RSI/BOLL/MACD/量比) | ✓ | ✓ | ✓ | ✓ |
| 主力资金流 | 暂停：旧外部源已禁用 | 暂停 | 暂停 | 暂停 |
| 龙虎榜 | 暂停：旧外部源已禁用 | ✗ | ✗ | ✗ |
| 新闻 + LLM 情感分析 | akshare 新闻 → **LLM 兜底** | akshare 新闻 → **LLM 兜底** | ✗ ETF 无个股新闻 | Finnhub company-news → **LLM 兜底**（英文新闻 Claude 双语处理） |
| **期权流 (PCR/IV)** | 暂停：旧外部源已禁用 | 暂停 | 暂停 | 暂停 |
| **多因子打分卡** | 仅技术因子 | 仅技术因子 | 仅技术因子 | 仅技术因子 |

最终给加权信号合成 → 下一交易日开盘倾向（看多/偏多/震荡/偏空/看空 + 置信度）+ **JSON 报告 + PDF 研报（含雷达图）**。

### 多因子打分卡（当前版）

当前按“Tickflow + yfinance + akshare新闻 + Finnhub新闻”收敛数据源后，多因子仅保留由 K 线派生的技术因子，避免触发旧估值、财务、指数等外部接口。

| 类别 | 权重 | 因子内容 | 数据源 |
|---|---|---|---|
| **技术因子** | 100% | 5 日动量 / 20 日动量 / 20 日波动率分位 / 量比 / 布林位置 | Tickflow K 线 |

合成后乘 0.8 进总信号 aggregator（避免与已有技术 / 当日涨跌信号过度叠加）。

PDF 报告中：因子卡章节保留技术因子概览表；风格、基本面、相对强度因子当前暂停。

### 因子有效性检验

当前仅对 K 线技术因子做时序 IC / IR 检验；旧风格因子、财务因子、指数相对强度因子因外部源禁用而不再计算。

### 期权流 PCR / 隐含波动率

期权流旧外部源已禁用，当前主流程中该信号固定记 0 分并标注“期权流外部源已禁用，跳过”。后续如 Tickflow 提供期权链或 PCR/IV 口径，再恢复该模块。

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

如果用户问“沪深300ETF”“中概互联ETF”等名称但没给代码，优先用常识映射；不确定时直接向用户确认代码。

### 美股识别

美股是字母代码（1-5 个大写字母，可能含 `.`）。用户给代码时直接转大写传 `us`：
- `AAPL` Apple / `MSFT` Microsoft / `NVDA` Nvidia / `TSLA` Tesla / `META` Meta / `AMZN` Amazon
- `GOOGL` Alphabet / `BRK.B` Berkshire B 类 / `BABA` 阿里 ADR / `JD` 京东 ADR

如果用户给中文名（"特斯拉""英伟达""苹果""阿里""谷歌"等），让 Claude 直接对应到 ticker（这些是常识级映射）。

### 数据源与环境

> 推荐解释器：`~/.pyenv/versions/dev-3.12/bin/python`。如果环境变量未设置，可先执行：
>
> ```bash
> export STOCK_ANALYST_PYTHON="$HOME/.pyenv/versions/dev-3.12/bin/python"
> ```
>
> 核心依赖安装：`$STOCK_ANALYST_PYTHON -m pip install -r <SKILL_DIR>/requirements.txt`。
>
> **Tickflow 行情主源**：代码内置用户提供的 Tickflow token，同时支持 `STOCK_ANALYST_TICKFLOW_TOKEN` 覆盖。接口可用性用 `python scripts/probe_data_sources.py` 验证。
>
> **yfinance**：仅用于美股盘前/盘后 prepost 数据，通常需要代理才能稳定访问。代理设置：`STOCK_ANALYST_PROXY=http://127.0.0.1:7890` 或 macOS 系统代理。
>
> **Finnhub 美股新闻**：代码内置用户提供的 Finnhub token，同时支持 `STOCK_ANALYST_FINNHUB_TOKEN` 覆盖。
>
> **LLM 新闻兜底**：当 akshare / Finnhub 新闻接口取空时，脚本会调 Claude（沿用 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 配置）列出近期新闻 JSON。

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

1. **代码**：用户给名字的话，优先用常识映射；不确定就问用户确认代码。不要为了补代码额外调用旧外部数据源。
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
- 技术形态特征（多头/空头/超买超卖）
- **因子打分**（当前仅技术因子；指出最强项与最弱项）
- LLM 情感判断的核心理由（如果有）
- 总分 + 走势倾向 + 置信度
- 置信度低或信号矛盾时，明确告诉用户
- **PDF 路径**（用户可能想直接打开或转发）

**对 ETF**：新闻情感、资金流、龙虎榜、期权流和基本面/风格因子当前暂停，所以信号主要来自 Tickflow K 线、当日涨跌、技术指标和技术因子。

**对港股**：资金流、龙虎榜、期权流和基本面/风格因子当前暂停，主要信号来自 Tickflow K 线、技术指标、技术因子和 akshare 新闻情感。

**对美股**：资金流、龙虎榜、期权流和基本面/风格因子当前暂停，主要信号来自 Tickflow K 线、技术指标、技术因子和 Finnhub 新闻情感；盘前/盘后实时数据用 yfinance prepost，可能需要代理。

## 数据源/调用细节

当前主流程只保留以下外部数据源：

| 模块 | 数据源 | 说明 |
|---|---|---|
| 行情 / K 线 / 基本信息 | Tickflow | `TickFlow.free()` / token 初始化，`klines.get(..., period="1d")` 与 `instruments.batch(...)` |
| 美股盘前 / 盘后实时 | yfinance | `Ticker.history(period="1d", interval="1m", prepost=True)`，仅非美股正常盘时用于实时展示 |
| A 股 / 港股新闻 | akshare | `ak.stock_news_em(symbol=code)` |
| 美股新闻 | Finnhub | `/company-news`，近 7 日公司新闻 |
| LLM 新闻兜底 / 情感分析 / PDF 综述 | Claude API | 主新闻源为空时兜底；新闻情感和 PDF 综合解读继续使用 |

旧的东财/新浪/雪球/同花顺/SEC/期权链/itick 等外部接口已从主流程禁用；资金流、龙虎榜、期权流、公告信息面固定记 0 分并显示“外部源已禁用，跳过”。

详见：
- [data_sources.md](references/data_sources.md) — 历史数据源备忘，当前实现以本节为准
- [signal_logic.md](references/signal_logic.md) — 信号评分规则、聚合权重、走势映射阈值

## 信号合成顺序

```
当日涨跌 + 技术指标(×0.7) + LLM 情感(×置信度) + 多因子合成(仅技术因子, weighted ×0.8)
    + 资金流(暂停,0) + 龙虎榜(暂停,0) + 期权流(暂停,0) + 信息面(暂停,0)
↓
信号合计 → 走势倾向（看多/偏多/弱偏多/震荡/弱偏空/偏空/看空）+ 置信度
```

## 故障排查

### 首次安装报错 "ModuleNotFoundError"

跑 `pip install -r <SKILL_DIR>/requirements.txt`，再 `python <SKILL_DIR>/scripts/check_env.py` 验证。
如果用户有特定 Python 环境（pyenv/conda/venv），让他们 `export STOCK_ANALYST_PYTHON=/path/to/python` 然后再调用 skill。

### Tickflow 行情失败

症状：基本信息 / K 线为空，或输出 `Tickflow K 线失败` / `Tickflow 标的信息失败`。

排查 / 应对：
1. 确认依赖安装在 `dev-3.12`：`~/.pyenv/versions/dev-3.12/bin/python -m pip show tickflow`
2. 跑 `~/.pyenv/versions/dev-3.12/bin/python scripts/probe_data_sources.py` 查看 Tickflow、yfinance、akshare、Finnhub 哪个失败
3. 检查 symbol 格式：A 股 `600000.SH` / `000001.SZ`，港股 `700.HK`，美股 `AAPL`
4. 如果 token 权限不足，可临时 `export STOCK_ANALYST_TICKFLOW_TOKEN=...` 覆盖内置 token

### yfinance 盘前 / 盘后失败

`yfinance` 只用于美股盘前 / 盘后实时数据；失败不影响历史 K 线主流程。

排查 / 应对：
1. 优先打开代理（`STOCK_ANALYST_PROXY` 或 macOS 系统代理）
2. 同一 ticker 短时间连续重试可能被 Yahoo 限速，建议等 5-10 分钟
3. 若只是历史分析，不必强依赖 yfinance prepost

### Finnhub 新闻失败

症状：美股新闻为空，或日志出现 `Finnhub news 失败`。

排查 / 应对：
1. 跑 `python scripts/probe_data_sources.py` 看 `finnhub news US` 是否通过
2. 如 token 过期或额度不足，用 `STOCK_ANALYST_FINNHUB_TOKEN` 覆盖
3. 主源为空时会自动走 LLM 新闻兜底

### LLM 情感分析失败

如果输出 `LLM情感: 分析失败: ...`：
- `anthropic SDK 未安装`：`pip install anthropic`
- `模型输出无法解析为 JSON`：偶发，重跑一次通常 OK
- `Connection error / Server disconnected`：检查 `ANTHROPIC_BASE_URL` 是否可达；如果是内网网关而本机有系统代理，脚本默认 `trust_env=False` 已绕过，但极端情况可显式 `export ANTHROPIC_HTTP_PROXY=http://...`

### PDF 没生成（只有 HTML）

Chrome 没装或路径不对。装 Chrome / Chromium / Edge 任一，或 `export CHROME_BIN=/path/to/chrome`。
跑 `python scripts/check_env.py` 看是否能正确探测到。

### 目标日无 K 线

最常见原因：用户给的日期是周末/节假日/停牌日。脚本会打印“目标日无 K 线”。
处理：自动回退到前一个交易日，或问用户确认目标日。

### 北交所代码（4/8 开头）

不支持。告诉用户：本 skill 仅覆盖沪深 A 股/港股主流票/ETF/美股，北交所数据接口不同，暂未实现。

### 港股代码格式

港股输入给 5 位（带前导 0）：小米 → `01810`，腾讯 → `00700`，比亚迪 → `01211`。Tickflow 内部会转成 `1810.HK` / `700.HK`。

### LLM 新闻兜底没有结果

- `anthropic SDK 未安装` — `pip install anthropic`
- `LLM 新闻 JSON 解析失败` — 模型偶发返回非 JSON，重跑通常 OK
- 模型联网能力弱时返回的可能全是过期新闻，建议把这类输出当作“补充上下文”而非“最新事件”

## 局限

- 仅作流程演示，**不构成投资建议**
- 技术指标对 1-3 天的短期预测可靠性有限
- 情感分析依赖 LLM 判断，对小道消息/谣言识别不稳定
- 资金流、龙虎榜、期权流、公告/SEC 信息面当前已禁用，相关信号固定为 0
- ETF 当前无新闻情感，信号偏少
- 美股盘前/盘后实时依赖 yfinance，可能受代理和 Yahoo 限速影响
- 未考虑大盘 beta / 行业政策 / 板块轮动 / 衍生品持仓 / 公司治理

每次给用户结果时附上一句免责声明。
