# stock-analyst

A 股 / 港股 / ETF / 美股 综合分析与短期走势预测 — Claude Code skill。

输入：股票/ETF 代码 + 目标交易日。
输出：技术指标 + 资金流 + 龙虎榜 + 新闻情感（Claude API）→ 加权信号合成 → 下一交易日开盘倾向 + JSON 报告 + **PDF 研报**。

## 安装

### 1. 把整个目录放到 `~/.claude/skills/stock-analyst/`

```bash
# macOS / Linux
mkdir -p ~/.claude/skills
cp -r stock-analyst ~/.claude/skills/

# 或者解压到 ~/.claude/skills/stock-analyst/
unzip stock-analyst.zip -d ~/.claude/skills/
```

Claude Code 启动后会自动发现这个 skill。

### 2. 装 Python 依赖

需要 **Python 3.10+**。建议用 venv 隔离：

```bash
cd ~/.claude/skills/stock-analyst
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

或者直接装到当前环境：

```bash
pip install -r ~/.claude/skills/stock-analyst/requirements.txt
```

> **告诉 skill 用哪个 Python**：默认用 PATH 上的 `python3`。如果要指定其他解释器（如 pyenv 的某个 env），设环境变量：
> ```bash
> export STOCK_ANALYST_PYTHON=/path/to/python
> ```

### 3. 装 Chrome（用于生成 PDF）

PDF 走 Chrome headless 渲染。已支持的发现路径：

- **macOS**: `/Applications/Google Chrome.app`（默认）
- **Linux**: `/usr/bin/google-chrome` / `/usr/bin/chromium-browser` / `snap install chromium`
- **Windows**: `C:\Program Files\Google\Chrome\Application\chrome.exe`
- **自定义**: `export CHROME_BIN=/path/to/chrome`

Chrome 不存在时会降级输出 HTML（不影响 JSON 报告）。

### 4. 配置环境变量

#### 必需（情感分析与综合解读用）

```bash
export ANTHROPIC_API_KEY="sk-..."           # 或者 ANTHROPIC_AUTH_TOKEN
# 可选：自托管 / 网关
export ANTHROPIC_BASE_URL="https://api.anthropic.com"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-opus-4-7"
```

如果不设，情感分析 + PDF 综合解读会跳过，但其他分析仍可跑。

#### 可选

```bash
# 美股需要代理才能稳定访问 Yahoo Finance（仅作用于 yfinance，不影响国内接口）
export STOCK_ANALYST_PROXY="http://127.0.0.1:7890"

# 走代理调 Anthropic（默认 trust_env=False 跳过系统代理；只在 BASE_URL 是公网时设）
export ANTHROPIC_HTTP_PROXY="http://127.0.0.1:7890"
```

### 5. 验证安装

```bash
python ~/.claude/skills/stock-analyst/scripts/check_env.py
```

期望输出全 ✓（warning 项不阻断）。

## 用法

### 通过 Claude Code（推荐）

直接在 Claude Code 中输入自然语言，Claude 会自动调用 skill：

- "分析一下美股 NVDA"
- "瑞芯微 5/22 怎么走"
- "沪深300ETF 最近怎么样"
- "小米集团 1810 帮我看看"

### 直接命令行

```bash
# A 股
python scripts/analyze.py 603893 sh 20260522 瑞芯微 --out ./output

# 港股（5 位代码）
python scripts/analyze.py 01810 hk 20260522 小米集团 --out ./output

# ETF（市场写 etf 即可）
python scripts/analyze.py 510300 etf 20260522 沪深300ETF --out ./output

# 美股
python scripts/analyze.py NVDA us 20260526 Nvidia --out ./output
```

参数：
| 参数 | 说明 |
|---|---|
| code | A 股 6 位 / 港股 5 位（带前导 0）/ ETF 6 位 / 美股字母代码（全大写） |
| market | `sh` / `sz` / `hk` / `etf` / `us` |
| target_date | YYYYMMDD（必须是已收盘交易日） |
| name | 名称（可选，仅用于显示） |
| `--out` | 输出目录，默认 `./output` |

每次运行会在 `--out/` 下生成两份报告：

```
report_{code}_{date}.json   # 结构化原始数据
report_{code}_{date}.pdf    # 中文研报样式 PDF（含 Claude 综合解读）
```

## 故障排查

### 跑 check_env.py 显示有依赖 missing

```bash
pip install -r requirements.txt
```

### Anthropic API 连通失败

- 检查 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` 是否正确
- 如果 BASE_URL 指向公网，确认网络能访问；如果是内网网关，**不要走系统代理**（脚本默认 `trust_env=False`）
- 如果你的网关需要 HTTP 代理：`export ANTHROPIC_HTTP_PROXY=http://...`

### 美股 yfinance 失败

- 确保设了 `STOCK_ANALYST_PROXY` 或 macOS 系统代理
- Yahoo Finance 限速：等 5-10 分钟再试
- 错误：`'data' KeyError` / 空响应：换代理节点

### A 股资金流报错（`RemoteDisconnected`）

`push2.eastmoney.com` 短期风控。脚本已自动 fallback 到同花顺/雪球；多个数据源同时失效时换网络重试。

### PDF 生成失败 → 只有 HTML

- 确认 Chrome 已安装；或手动 `export CHROME_BIN=/path/to/chrome`
- 跑 `check_env.py` 看 Chrome 是否被正确探测

### 北交所代码 4/8 开头

不支持，本 skill 仅覆盖沪深 A 股 / 港股主流票 / ETF / 美股。

## 目录结构

```
stock-analyst/
├── SKILL.md              # Skill metadata + 给 Claude 的调用说明
├── README.md             # 本文件（给人看）
├── requirements.txt      # Python 依赖
├── scripts/
│   ├── analyze.py        # 主分析脚本
│   ├── data_layer.py     # 多源数据获取（akshare + yfinance）
│   ├── technical.py      # 技术指标
│   ├── fund_flow.py      # 资金流（A 股）
│   ├── sentiment_llm.py  # Claude API 新闻情感
│   ├── report_pdf.py     # PDF 渲染（Markdown → Chrome headless → PDF）
│   ├── check_env.py      # 环境自检
│   └── probe_data_sources.py  # 数据源连通性探测
├── references/           # 数据源 / 信号逻辑文档
└── output/               # 输出目录（JSON + PDF）
```

## 局限

- **仅作流程演示，不构成投资建议**
- 技术指标对 1-3 天预测可靠性有限
- 港股 / 美股没有个股资金流口径
- ETF 无新闻情感
- 北交所暂不支持
- 未考虑大盘 beta / 行业政策 / 板块轮动 / 衍生品

---

License: 自用工具，无明确开源协议；分发请自行处理。
