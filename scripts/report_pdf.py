"""把 analyze.py 生成的 report dict 渲染成 Markdown → PDF。

PDF 路径：{out_dir}/report_{code}_{date}.pdf
依赖：markdown 包 + Chrome headless（macOS 默认路径）+ matplotlib（雷达图）。

设计：
- 结构化数据全部用表格呈现（基本信息、当日价、技术快照、新闻事件、信号汇总）
- 因子打分卡：4 维雷达图 + 因子明细表
- PDF 默认只渲染 JSON；可用 --with-synthesis 显式执行可选 LLM 增强
- A4 / 中文衬线 / 表格 / 引用块样式贴近金融研报
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import markdown

from llm_client import LLMError, generate_text, get_llm_status


def _find_chrome() -> str | None:
    """跨平台寻找 Chrome / Chromium / Edge。优先环境变量 CHROME_BIN。"""
    env = os.environ.get("CHROME_BIN", "").strip()
    if env and os.path.exists(env):
        return env
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    elif sys.platform.startswith("linux"):
        candidates += [
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge", "/snap/bin/chromium",
        ]
    elif sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        candidates += [
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pfx86}\Google\Chrome\Application\chrome.exe",
            rf"{pf}\Microsoft\Edge\Application\msedge.exe",
            rf"{pfx86}\Microsoft\Edge\Application\msedge.exe",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    for cmd in ("google-chrome", "chromium", "chromium-browser", "chrome", "microsoft-edge"):
        which = shutil.which(cmd)
        if which:
            return which
    return None

# ---------- helpers ----------

def _fmt_num(v: Any, decimals: int = 2) -> str:
    if v is None or v == "":
        return "-"
    try:
        f = float(v)
        if f >= 1e12:
            return f"{f/1e12:.2f} 万亿"
        if f >= 1e8:
            return f"{f/1e8:.2f} 亿"
        if f >= 1e4 and abs(f) < 1e8:
            return f"{f/1e4:.2f} 万"
        return f"{f:.{decimals}f}"
    except Exception:
        return str(v)


def _safe_llm_detail(value: Any) -> str:
    """Do not leak provider authentication exceptions into user reports."""
    text = str(value or "")
    auth_markers = (
        "Could not resolve authentication method",
        "Expected one of api_key",
        "ANTHROPIC_API_KEY",
    )
    if any(marker in text for marker in auth_markers):
        return "未配置 LLM 凭证，已跳过可选增强"
    return text


def _kind_label(kind: str) -> str:
    return {"astock": "A 股", "hk": "港股", "etf": "ETF", "us": "美股"}.get(kind, kind)


def _verdict_emoji(verdict: str) -> str:
    if "看多" in verdict: return "📈"
    if "偏多" in verdict: return "↗"
    if "看空" in verdict: return "📉"
    if "偏空" in verdict: return "↘"
    return "→"


# ---------- markdown sections ----------

def _section_header(report: dict) -> str:
    name = report.get("name", "")
    code = report.get("code", "")
    kind = report.get("kind", "")
    target = report.get("target_date", "")
    target_dash = f"{target[:4]}-{target[4:6]}-{target[6:]}" if len(target) == 8 else target
    next_open = report.get("verdict", {}).get("next_open", "—")
    return (
        f"# {name} ({code}) 综合分析报告\n\n"
        f"> **{_kind_label(kind)}**｜分析目标日 **{target_dash}**｜"
        f"下一交易日 **{next_open}**\n\n"
    )


def _section_info(report: dict) -> str:
    info = report.get("info") or {}
    if not info:
        return ""
    rows = []
    KEY_ORDER = ["名称", "代码", "行业", "市值", "市盈率TTM", "市净率",
                 "最新价", "52周高", "52周低", "交易所", "总股本", "流通股本",
                 "净资产", "营业收入", "净利润", "毛利率"]
    seen = set()
    for k in KEY_ORDER:
        if k in info and info[k] not in (None, "", "-"):
            v = info[k]
            if k in ("市值", "总股本", "流通股本", "营业收入", "净利润", "净资产"):
                v = _fmt_num(v)
            elif k in ("市盈率TTM", "市净率"):
                v = f"{float(v):.2f}x" if v else "-"
            else:
                v = str(v)[:120]
            rows.append(f"| {k} | {v} |")
            seen.add(k)
    extras = [k for k in info if k not in seen and info[k] not in (None, "", "-") and "简介" not in k][:6]
    for k in extras:
        rows.append(f"| {k} | {str(info[k])[:120]} |")
    if not rows:
        return ""
    md = "## 一、基本信息\n\n| 项目 | 数值 |\n|---|---|\n" + "\n".join(rows) + "\n\n"
    intro = info.get("简介") or info.get("公司简介")
    if intro:
        md += f"**公司简介**：{str(intro)[:400]}…\n\n"
    return md


def _fmt_quote_time(ts_ms: Any) -> str:
    """毫秒时间戳 → 本地时间字符串。"""
    try:
        from datetime import datetime as _dt
        return _dt.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def _section_realtime(report: dict) -> str:
    """盘中实时报价（提问时点抓取）。仅当日分析才有此字段。"""
    rt = report.get("realtime") or {}
    if not rt or rt.get("price") is None:
        return ""
    chp = rt.get("change_pct")
    arrow = "📈" if (chp or 0) > 0 else ("📉" if (chp or 0) < 0 else "→")
    chp_s = f"{arrow} {chp:+.2f}%" if isinstance(chp, (int, float)) else "—"
    queried = rt.get("queried_at") or "—"
    quote_t = _fmt_quote_time(rt.get("ts_ms"))
    source = rt.get("source") or rt.get("region") or "行情源"
    md = f"> ⚡ **盘中实时报价**（{source} · 提问时点 **{queried}**，交易所报价时间 {quote_t}）\n\n"
    md += "| 项目 | 数值 |\n|---|---|\n"
    md += f"| **最新价** | **{_fmt_num(rt.get('price'))}** |\n"
    md += f"| **涨跌幅** | **{chp_s}** |\n"
    md += f"| 涨跌额 | {_fmt_num(rt.get('change'))} |\n"
    md += f"| 开 / 高 / 低 | {_fmt_num(rt.get('open'))} / {_fmt_num(rt.get('high'))} / {_fmt_num(rt.get('low'))} |\n"
    md += f"| 成交量 | {_fmt_num(rt.get('volume'), 0)} |\n"
    md += f"| 成交额 | {_fmt_num(rt.get('turnover'), 0)} |\n"
    md += "\n"
    return md


def _section_price(report: dict) -> str:
    p = report.get("price") or {}
    if not p:
        return ""
    open_, high, low, close = p.get("open"), p.get("high"), p.get("low"), p.get("close")
    vol, prev, pct = p.get("volume"), p.get("prev_close"), p.get("pct_change")
    arrow = "📈" if (pct or 0) > 0 else ("📉" if (pct or 0) < 0 else "→")
    md = "## 二、当日价格表现\n\n"
    rt_block = _section_realtime(report)
    md += rt_block
    if rt_block:
        md += "### 收盘 / 目标交易日\n\n"
    md += "| 项目 | 数值 |\n|---|---|\n"
    md += f"| 开盘 | {_fmt_num(open_)} |\n"
    md += f"| 最高 | {_fmt_num(high)} |\n"
    md += f"| 最低 | {_fmt_num(low)} |\n"
    md += f"| **收盘** | **{_fmt_num(close)}** |\n"
    md += f"| 成交量 | {_fmt_num(vol, 0)} 股 |\n"
    md += f"| 前收 | {_fmt_num(prev)} |\n"
    if pct is not None:
        md += f"| **涨跌幅** | **{arrow} {pct:+.2f}%** |\n"
    md += "\n"
    kline = report.get("recent_kline") or []
    if kline:
        md += "### 近 6 个交易日\n\n"
        md += "| 日期 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |\n|---|---|---|---|---|---|\n"
        for r in kline[-6:]:
            d = str(r.get("date", ""))[:10]
            md += (f"| {d} | {_fmt_num(r.get('open'))} | {_fmt_num(r.get('high'))} | "
                   f"{_fmt_num(r.get('low'))} | **{_fmt_num(r.get('close'))}** | "
                   f"{_fmt_num(r.get('volume'), 0)} |\n")
        md += "\n"
    return md


def _section_technical(report: dict) -> str:
    t = report.get("technical") or {}
    if not t:
        return ""
    snap = t.get("snapshot") or {}
    rules = t.get("rules") or []
    md = "## 三、技术指标\n\n"
    if snap:
        md += "| 指标 | 数值 |\n|---|---|\n"
        rows = [
            ("收盘价", snap.get("close")),
            ("MA5", snap.get("MA5")),
            ("MA10", snap.get("MA10")),
            ("MA20", snap.get("MA20")),
            ("MA60", snap.get("MA60")),
            ("RSI(14)", snap.get("RSI14")),
            ("布林上轨", snap.get("BB_up")),
            ("布林中轨", snap.get("BB_mid")),
            ("布林下轨", snap.get("BB_dn")),
            ("布林%B", snap.get("BB_pct")),
            ("MACD 柱", snap.get("MACD_hist")),
            ("量比(5日)", snap.get("VOL_RATIO")),
        ]
        for k, v in rows:
            if v is None: continue
            if k == "RSI(14)" or k.startswith("MA") or k.startswith("布林上") or k.startswith("布林中") or k.startswith("布林下") or k == "收盘价":
                md += f"| {k} | {_fmt_num(v)} |\n"
            elif k == "布林%B":
                md += f"| {k} | {float(v):.3f} |\n"
            else:
                md += f"| {k} | {_fmt_num(v, 3)} |\n"
        md += "\n"
    if rules:
        md += "**触发规则**：\n\n"
        for r in rules:
            md += f"- {r}\n"
        md += f"\n**技术面原始得分**：{t.get('score_raw', 0):+.2f}\n\n"
    return md


def _section_fund_flow(report: dict) -> str:
    ff = report.get("fund_flow")
    kind = report.get("kind", "")
    md = "## 四、资金流\n\n"
    if not ff or ff.get("available") is False:
        if kind == "astock":
            md += "> 东财、雪球和同花顺均未返回可用资金流数据。该项按缺失处理，不代表净流入为零。\n\n"
        elif kind == "etf":
            md += "> ETF 资金流快照不在目标日，跳过\n\n"
        else:
            md += f"> {_kind_label(kind)}无个股资金流口径，跳过\n\n"
        return md
    md += "| 项目 | 数值 |\n|---|---|\n"
    for k in ("source", "main_net", "main_buy", "main_sell", "snapshot_date"):
        if k in ff and ff[k] is not None:
            label = {"source": "数据源", "main_net": "主力净流入",
                     "main_buy": "主力买入", "main_sell": "主力卖出",
                     "snapshot_date": "快照日"}.get(k, k)
            v = ff[k]
            if isinstance(v, (int, float)):
                v = _fmt_num(v) + " 元"
            md += f"| {label} | {v} |\n"
    md += "\n"
    return md


def _section_lhb(report: dict) -> str:
    lhb = report.get("lhb")
    if not lhb:
        return ""
    md = "## 五、龙虎榜\n\n"
    if isinstance(lhb, list) and lhb:
        for row in lhb[:5]:
            md += f"- 上榜原因：{row.get('上榜原因', '-')}｜买入额：{_fmt_num(row.get('买方机构买入额'))}｜卖出额：{_fmt_num(row.get('卖方机构卖出额'))}\n"
        md += "\n"
    else:
        md += "> 当日未上榜\n\n"
    return md


def _section_sentiment(report: dict) -> str:
    s = report.get("sentiment") or {}
    if s.get("skipped"):
        return f"## 六、新闻情感\n\n> {s.get('reason', 'LLM 增强未启用，已跳过')}\n\n"
    if not s or "error" in s and not s.get("events"):
        md = "## 六、新闻情感\n\n"
        if s.get("error"):
            md += f"> {_safe_llm_detail(s['error'])}\n\n"
        else:
            md += "> 无新闻数据\n\n"
        return md
    provider = s.get("_provider") or "LLM"
    provider_label = "本地规则" if provider == "local-rules" else provider
    md = f"## 六、新闻情感（{provider_label} 分析）\n\n"
    md += f"**整体判断**：`{s.get('verdict', 'neutral')}`｜置信度：`{s.get('confidence', 'low')}`｜原始得分：**{s.get('score', 0):+.1f} / 5**\n\n"
    rationale = s.get("rationale") or ""
    if rationale:
        md += f"> {rationale}\n\n"
    if s.get("_fallback_reason"):
        md += "> 注：LLM 不可用，本节由本地可解释规则模型生成。\n\n"
    events = s.get("events") or []
    if events:
        md += "### 核心事件\n\n"
        md += "| 方向 | 重要性 | 时效 | 摘要 | 来源数 |\n|---|---|---|---|---|\n"
        for e in events:
            direction = e.get("direction", "?")
            badge = "🔴" if direction == "-" else ("🟢" if direction == "+" else "⚪")
            md += (f"| {badge} {direction} | {e.get('importance','?')}/5 | "
                   f"{e.get('timing','-')} | {e.get('summary','')[:80]} | "
                   f"{e.get('sources_count', 1)} |\n")
        md += "\n"
    return md


def _radar_png_base64(category_scores: dict) -> str:
    """生成 4 维因子雷达图（PNG → base64 data URI）。category_scores: {key: -1..+1}。

    matplotlib 不可用时返回空串。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        return ""

    # 中文字体
    for fp in [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]:
        if os.path.exists(fp):
            try:
                font_manager.fontManager.addfont(fp)
                # 抓字体族名
                fp_obj = font_manager.FontProperties(fname=fp)
                plt.rcParams["font.family"] = fp_obj.get_name()
                break
            except Exception:
                pass
    plt.rcParams["axes.unicode_minus"] = False

    cat_zh = {"technical": "技术", "style": "风格",
              "fundamental": "基本面", "relative_strength": "相对强度"}
    keys = ["technical", "style", "fundamental", "relative_strength"]
    labels = [cat_zh[k] for k in keys]
    # 把 -1..+1 映射到 0..1（中心 0.5 对应 0 分）
    raw = [float(category_scores.get(k, 0)) for k in keys]
    values = [(v + 1) / 2 for v in raw]

    import math
    angles = [n / float(len(keys)) * 2 * math.pi for n in range(len(keys))]
    values_loop = values + values[:1]
    angles_loop = angles + angles[:1]

    fig, ax = plt.subplots(figsize=(4.6, 4.6), subplot_kw={"polar": True}, dpi=160)
    ax.set_theta_offset(math.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontsize=11)
    ax.set_ylim(0, 1)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["-0.5", "0", "+0.5", "+1.0"], fontsize=8, color="#888")
    ax.grid(color="#aaa", alpha=0.4, linestyle="--", linewidth=0.6)

    ax.plot(angles_loop, values_loop, linewidth=2.0, color="#2c5aa0")
    ax.fill(angles_loop, values_loop, alpha=0.25, color="#4a90d9")

    # 在每个点上标注原始分
    for ang, raw_v, val in zip(angles, raw, values):
        color = "#c53030" if raw_v < -0.1 else ("#22863a" if raw_v > 0.1 else "#5a5a5a")
        ax.text(ang, val + 0.08, f"{raw_v:+.2f}",
                ha="center", va="center", fontsize=9.5,
                fontweight="bold", color=color)

    ax.set_title("因子雷达图", fontsize=12, color="#1a365d", pad=18)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _grade_badge(grade: str | None) -> str:
    return {
        "A": "🟢 A",
        "B": "🟢 B",
        "C": "🟡 C",
        "D": "🔴 D",
        "?": "⚪ ?",
    }.get(grade, "—")


def _section_factor_validation(f: dict) -> str:
    """渲染因子有效性检验汇总表。"""
    val = f.get("validation") or {}
    results = val.get("results") or {}
    summary = val.get("summary") or {}
    if not results:
        return ""

    md = "### 因子有效性检验\n\n"
    grades = summary.get("grades") or {}
    md += (f"> **检验方法**：单股时序 IC（Spearman, 5 日远期收益）+ 60 日滚动 IR；"
           f"阈值 |IC|≥0.06 + |IR|≥0.5 通过。"
           f"A:{grades.get('A',0)} | B:{grades.get('B',0)} | "
           f"C:{grades.get('C',0)}（半权重） | D:{grades.get('D',0)}（剔除）；"
           f"**通过率 {summary.get('passed',0)}/{summary.get('total',0)}**，"
           f"平均 |IC|={summary.get('mean_abs_IC')}，平均 |IR|={summary.get('mean_abs_IR')}\n\n")
    md += "| 因子 | 样本数 | IC | IR | 评级 | 处置 |\n|---|---|---|---|---|---|\n"
    # 按评级排序：A → B → C → D → ?
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "?": 4}
    sorted_keys = sorted(results.keys(),
                         key=lambda k: (order.get(results[k].get("grade", "?"), 5),
                                        -abs(results[k].get("abs_IC") or 0)))
    name_zh = {
        "tech_momentum_5d": "技术·5 日动量",
        "tech_momentum_20d": "技术·20 日动量",
        "tech_volatility": "技术·20 日波动率",
        "tech_vol_ratio": "技术·量比",
        "tech_boll_pct": "技术·布林位置",
        "style_pe_pct_3y": "风格·PE 历史分位",
        "style_pb_pct_3y": "风格·PB 历史分位",
        "rs_vs_index_20d": "相对强度·vs 上证 20 日",
    }
    for k in sorted_keys:
        r = results[k]
        ic = r.get("IC")
        ir = r.get("IR")
        grade = r.get("grade", "?")
        ic_str = f"{ic:+.4f}" if ic is not None else "—"
        ir_str = f"{ir:+.4f}" if ir is not None else "—"
        if grade in ("A", "B"):
            disp = "✓ 全权计入"
        elif grade == "C":
            disp = "△ 半权重"
        elif grade == "D":
            disp = "✗ **剔除**"
        else:
            disp = r.get("note", "—")
        md += (f"| {name_zh.get(k, k)} | {r.get('samples','—')} | {ic_str} | {ir_str} | "
               f"{_grade_badge(grade)} | {disp} |\n")
    md += "\n"
    md += ("> **静态因子**（财务指标 ROE/毛利率/资产负债率/净利同比/营收同比、PEG、"
           "vs 行业当日 RS）样本量不足以做时序 IC，按原权重保留。\n\n")
    return md


def _section_factors(report: dict) -> str:
    """渲染多因子打分卡章节：雷达图 + 各类别明细表 + 有效性检验表 + 加权得分。"""
    f = report.get("factors") or {}
    if not f or "error" in f:
        if f and f.get("error"):
            return f"## 七、多因子打分卡\n\n> 因子计算失败：{f['error']}\n\n"
        return ""

    cat_scores = f.get("category_scores") or {}
    weighted = f.get("weighted_score", 0)
    weights = f.get("weights_used") or {}

    md = "## 七、多因子打分卡（含 IC/IR 有效性检验）\n\n"
    md += (f"**综合因子分**：`{weighted:+.2f}`（每类别归一化后按权重加权，"
           f"-1=最弱，+1=最强）\n\n")

    # 雷达图
    if cat_scores:
        png = _radar_png_base64(cat_scores)
        if png:
            md += f"<p style='text-align:center;'><img src='{png}' style='max-width:380px;'/></p>\n\n"

    # 类别概览表
    md += "### 类别得分概览\n\n"
    md += "| 类别 | 得分 (-1..+1) | 权重 | 保留因子 | 含义 |\n|---|---|---|---|---|\n"
    cat_zh = {
        "technical": "技术（量价/动量/波动）",
        "style": "风格（估值分位/PEG）",
        "fundamental": "基本面（ROE/毛利/同比）",
        "relative_strength": "相对强度（vs 行业/大盘）",
    }
    cat_data = f.get("categories") or {}
    for k in ("technical", "style", "fundamental", "relative_strength"):
        if k not in cat_scores:
            continue
        sc = cat_scores[k]
        w = weights.get(k, 0) * 100
        sym = "🟢" if sc > 0.1 else ("🔴" if sc < -0.1 else "⚪")
        cd = cat_data.get(k) or {}
        kept = cd.get("n_items_kept", "—")
        total = cd.get("n_items_total", "—")
        md += f"| {cat_zh.get(k, k)} | {sym} **{sc:+.2f}** | {w:.0f}% | {kept}/{total} | "
        md += ("偏多" if sc > 0.3 else "偏空" if sc < -0.3 else "中性") + " |\n"
    md += "\n"

    # 因子有效性检验表
    md += _section_factor_validation(f)

    # 各类别细项表（含每个因子的评级和实际计入分）
    for cat_key in ("technical", "style", "fundamental", "relative_strength"):
        cd = cat_data.get(cat_key)
        if not isinstance(cd, dict) or "items" not in cd:
            if cd and cd.get("error"):
                md += f"### {cat_zh.get(cat_key, cat_key)}：跳过\n\n> {cd['error']}\n\n"
            continue
        md += f"### {cat_zh.get(cat_key, cat_key)}\n\n"
        md += ("| 因子 | 数值 | 原始分 | 评级 | 计入分 | 解读 |\n"
               "|---|---|---|---|---|---|\n")
        for it in cd["items"]:
            v = it.get("validation") or {}
            grade = v.get("grade") if v else None
            mult = it.get("weight_multiplier", 1.0)
            raw = it.get("raw_score", it.get("score", 0))
            sc = it["score"]
            sym = "🟢" if sc > 0.1 else ("🔴" if sc < -0.1 else "⚪")
            if grade:
                grade_disp = _grade_badge(grade)
            elif it.get("validation_key") is None:
                grade_disp = "— 静态"
            else:
                grade_disp = "—"
            score_disp = (f"{sym} **{sc:+.2f}**" if mult > 0
                          else f"~~{raw:+.2f}~~ 剔除")
            md += (f"| {it['name']} | {it['value']} | {raw:+.2f} | "
                   f"{grade_disp} | {score_disp} | {it['remark']} |\n")
        md += "\n"

    return md


def _section_peers(report: dict) -> str:
    p = report.get("peers")
    if not p or "error" in p and not p.get("a_stock") and not p.get("us_stock"):
        return ""
    md = "## 八、板块联动 — 同行业看多标的\n\n"
    industry_zh = p.get("industry_zh") or "—"
    industry_en = p.get("industry_en") or ""
    industry_label = industry_zh + (f" / {industry_en}" if industry_en else "")
    stats = p.get("stats") or {}
    md += (f"**所属行业**：{industry_label}　|　"
           f"**筛选结果**：A 股 {stats.get('a_kept',0)}/{stats.get('a_total',0)}，"
           f"美股 {stats.get('us_kept',0)}/{stats.get('us_total',0)}"
           "（看涨且置信度 ≥ 中）\n\n")

    def _table(title: str, rows: list, code_key: str):
        if not rows:
            return f"### {title}\n\n> 无符合（看涨且置信度 ≥ 中）的标的\n\n"
        out = f"### {title}（按信号分降序）\n\n"
        out += "| 代码 | 名称 | 涨跌% | 信号分 | 判断 | 简评 |\n|---|---|---|---|---|---|\n"
        for r in rows:
            ticker = r.get(code_key) or r.get("code", "")
            name = r.get("name", "")
            pct = r.get("pct_change", 0)
            score = r.get("total_score", 0)
            verdict = f"{r.get('verdict','')}/{r.get('confidence','')}"
            reason = r.get("reason", "") or ""
            brief_rules = []
            for rule in r.get("tech_rules", []):
                head = rule.split(" → ")[0].strip()
                if any(k in head for k in ("多头排列", "金叉", "上轨", "放量上涨",
                                           "突破", "底背离", "超卖", "缩量")):
                    brief_rules.append(head)
                if len(brief_rules) >= 2: break
            tech_brief = "；".join(brief_rules) if brief_rules else "—"
            line = f"{tech_brief}"
            if reason:
                line = f"{tech_brief}<br/>**理由**: {reason}"
            out += (f"| `{ticker}` | {name} | **{pct:+.2f}%** | "
                    f"**{score:+.2f}** | {verdict} | {line} |\n")
        out += "\n"
        return out

    md += _table("A 股板块", p.get("a_stock") or [], "code")
    md += _table("美股板块", p.get("us_stock") or [], "ticker")
    return md


def _section_options(report: dict) -> str:
    o = report.get("options_flow")
    if not o:
        return ""
    md = "## 九、期权流（PCR / 隐含波动率）\n\n"
    if not o.get("available"):
        md += f"> {o.get('detail', '该标的无可交易期权，跳过')}\n\n"
        return md
    name = o.get("underlying_name") or o.get("underlying") or ""
    md += (f"**标的**：`{o.get('underlying','')}` {name}　|　"
           f"**来源**：{o.get('source','')}\n\n")
    md += "| 指标 | 数值 | 解读 |\n|---|---|---|\n"
    pcr_v = o.get("pcr_volume")
    pcr_o = o.get("pcr_oi")
    if pcr_v is not None:
        bias = "看涨占优" if pcr_v < 0.85 else ("认沽活跃/偏空" if pcr_v > 1.15 else "多空均衡")
        md += f"| PCR（成交量） | **{pcr_v}** | 认沽/认购成交比，{bias} |\n"
    if pcr_o is not None:
        md += f"| PCR（未平仓） | {pcr_o} | 存量仓位的认沽/认购比 |\n"
    if o.get("call_vol") is not None:
        md += f"| 认购 / 认沽成交量 | {_fmt_num(o.get('call_vol'),0)} / {_fmt_num(o.get('put_vol'),0)} | 当日 |\n"
    if o.get("call_oi") is not None:
        md += f"| 认购 / 认沽未平仓 | {_fmt_num(o.get('call_oi'),0)} / {_fmt_num(o.get('put_oi'),0)} | 存量 |\n"
    if o.get("iv") is not None:
        pct = o.get("iv_percentile_1y")
        chg = o.get("iv_chg_5d")
        extra = []
        if chg is not None:
            extra.append(f"5 日 {chg:+.2f}")
        if pct is not None:
            extra.append(f"1 年分位 {pct*100:.0f}%")
        md += f"| 隐含波动率 IV | {o.get('iv')} | {('，'.join(extra)) or '隐含波动率水平'} |\n"
    md += "\n"
    md += f"> **期权流信号**：`{o.get('score',0):+.2f}` — {o.get('detail','')}\n\n"
    return md


def _section_info_events(report: dict) -> str:
    """信息面:近期公告/年报/季报/事件(按影响力×情感排序 Top N)。"""
    sig = report.get("info_signal") or {}
    events = report.get("info_events") or []
    if not sig and not events:
        return ""
    md = "## 十、信息面（公告 / 年报 / 季报 / 事件）\n\n"
    if not sig.get("available", True):
        return md + "> 信息储备层暂覆盖 A 股 / 美股,该标的跳过。\n\n"
    md += (f"> **事件面信号**：`{sig.get('score', 0):+.2f}`（raw {sig.get('raw', 0):+.2f}，"
           f"窗口内 {sig.get('n_events', 0)} 条，已加工 {sig.get('n_processed', 0)} 条）\n\n")

    # 已加工、有方向的卡按 |情感×影响力| 排序取 Top
    scored = [e for e in events if e.get("sentiment") is not None and e.get("materiality") is not None]
    scored.sort(key=lambda e: abs(float(e["sentiment"]) * (float(e["materiality"]) + 0.1)), reverse=True)
    if scored:
        md += "| 日期 | 类型 | 影响力 | 情感 | 摘要 |\n|---|---|---|---|---|\n"
        for e in scored[:12]:
            sent = float(e["sentiment"])
            arrow = "↑" if sent > 0.1 else ("↓" if sent < -0.1 else "·")
            summ = (e.get("summary") or e.get("title") or "")[:48]
            md += f"| {e.get('event_date','')} | {e.get('type','')} | {e.get('materiality')} | {arrow}{sent:+.2f} | {summ} |\n"
        md += "\n"
    else:
        md += "> 窗口内无已加工事件。\n\n"
    return md


def _section_verdict(report: dict) -> str:
    v = report.get("verdict") or {}
    if not v:
        return ""
    md = "## 十一、综合判断 — 信号合计与走势倾向\n\n"
    signals = v.get("signals") or []
    if signals:
        md += "| 维度 | 得分 | 说明 |\n|---|---|---|\n"
        for s in signals:
            detail = _safe_llm_detail(s.get("detail", ""))[:120]
            md += f"| {s.get('name','')} | **{s.get('score',0):+.2f}** | {detail} |\n"
        md += "\n"
    score = v.get("score_total", 0)
    verdict = v.get("verdict", "—")
    conf = v.get("confidence", "—")
    next_open = v.get("next_open", "—")
    emoji = _verdict_emoji(verdict)
    md += f"### 最终判断 {emoji}\n\n"
    md += f"- **信号合计**：`{score:+.2f}`\n"
    md += f"- **下一交易日**（{next_open}）**开盘倾向**：**{verdict}**\n"
    md += f"- **置信度**：{conf}\n\n"
    return md


# ---------- 可选 LLM 综合解读（显式 enrichment 步骤） ----------

def generate_synthesis(report: dict) -> str:
    """Optional enrichment step. PDF rendering itself never calls an LLM."""
    status = get_llm_status()
    if not status.available:
        return ""

    payload = {
        "name": report.get("name"),
        "code": report.get("code"),
        "kind": report.get("kind"),
        "target_date": report.get("target_date"),
        "price": report.get("price"),
        "realtime": report.get("realtime"),  # 盘中实时报价（仅当日分析有；提问时点抓取）
        "technical": report.get("technical"),
        "fund_flow": report.get("fund_flow"),
        "sentiment": {
            "verdict": (report.get("sentiment") or {}).get("verdict"),
            "confidence": (report.get("sentiment") or {}).get("confidence"),
            "score": (report.get("sentiment") or {}).get("score"),
            "rationale": (report.get("sentiment") or {}).get("rationale"),
            "events": (report.get("sentiment") or {}).get("events"),
        },
        "options_flow": {k: v for k, v in (report.get("options_flow") or {}).items()
                         if k in ("available", "pcr_volume", "pcr_oi", "iv",
                                  "iv_chg_5d", "iv_percentile_1y", "score", "detail")},
        "verdict": report.get("verdict"),
        "info_events": {
            **{k: (report.get("info_signal") or {}).get(k) for k in ("score", "raw", "n_events", "available")},
            "top": (report.get("info_signal") or {}).get("top"),
        },
        "info_brief": {k: v for k, v in (report.get("info") or {}).items()
                       if k in ("市值", "市盈率TTM", "市净率", "52周高", "52周低", "行业")},
        "factors": {
            "weighted_score": (report.get("factors") or {}).get("weighted_score"),
            "category_scores": (report.get("factors") or {}).get("category_scores"),
            # 给 LLM 喂详情但裁剪冗余
            "detail": {
                k: {"score": v.get("category_score"),
                    "items": [
                        {"name": it["name"], "value": it["value"],
                         "score": it["score"], "remark": it["remark"]}
                        for it in (v.get("items") or [])
                    ]}
                for k, v in ((report.get("factors") or {}).get("categories") or {}).items()
                if isinstance(v, dict) and "items" in v
            },
        },
    }

    sys_prompt = """你是金融研报分析师。基于给定的结构化分析数据（已含技术指标、当日价、新闻事件、综合得分），为投资者写一段中文综合解读。

要求：
1. 给出 5-7 个小节，每节用 ## 标题。建议结构：
   - ## 主导逻辑（这次涨/跌的根本原因）
   - ## 技术面深读（量价 / 均线 / 超买超卖）
   - ## 因子诊断（综合解读 4 类因子打分：技术/风格/基本面/相对强度，指出最强项与最弱项）
   - ## 风险点（按权重列 3-5 条，结合因子分中分数为负的项）
   - ## 情景与概率（突破 / 震荡 / 回调，给百分比）
   - ## 操作建议（按持仓 / 空仓 / 风格分类）
2. 用 Markdown 表格、blockquote、加粗增强可读性
3. 不要重复前面的原始数据（PDF 已经有表格了），重在"分析"和"判断"
4. 务必结合实际数值（不要写"涨幅大"、要写"涨幅 +19.29%"；引用因子分时给出原始分如"技术因子 +0.40"）
5. 末尾不需要免责声明（PDF 模板会自动加）
6. 总长度 700-1400 字
7. 若数据含 `realtime` 字段（盘中实时报价，截至 queried_at 提问时点），说明这是**当下盘中价**而非收盘价：主导逻辑与操作建议须锚定该实时价，并指出它相对最近收盘的变化方向。无 realtime 字段时按收盘价分析即可。
"""

    try:
        response = generate_text(
            "结构化分析数据：\n" + json.dumps(payload, ensure_ascii=False, indent=2),
            system_prompt=sys_prompt,
            max_tokens=4096,
            retries=2,
            thinking=True,
        )
        return response.text
    except LLMError as exc:
        print(f"  [LLM] 综合解读失败，跳过：{exc}")
        return ""


# ---------- HTML / PDF ----------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 18mm 16mm; }}
  body {{
    font-family: -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    font-size: 11pt; line-height: 1.65; color: #222;
  }}
  h1 {{
    font-size: 22pt; border-bottom: 3px solid #2c5aa0;
    padding-bottom: 8px; margin-top: 0; color: #1a365d;
  }}
  h2 {{
    font-size: 15pt; color: #2c5aa0;
    border-left: 4px solid #2c5aa0; padding-left: 10px; margin-top: 26px;
  }}
  h3 {{ font-size: 12.5pt; color: #2d3748; margin-top: 18px; }}
  p {{ margin: 7px 0; }}
  table {{
    border-collapse: collapse; width: 100%;
    margin: 10px 0; font-size: 10pt;
  }}
  th, td {{
    border: 1px solid #cbd5e0; padding: 6px 9px;
    text-align: left; vertical-align: top;
  }}
  th {{ background-color: #ebf4ff; font-weight: 600; color: #1a365d; }}
  tr:nth-child(even) {{ background-color: #f7fafc; }}
  blockquote {{
    border-left: 4px solid #d69e2e; background: #fffaf0;
    margin: 10px 0; padding: 7px 13px; color: #5a4515;
  }}
  code {{
    background: #f1f5f9; padding: 2px 5px; border-radius: 3px;
    font-family: "SF Mono", "Menlo", monospace; font-size: 0.9em;
  }}
  strong {{ color: #c53030; }}
  ul, ol {{ padding-left: 22px; }}
  li {{ margin: 3px 0; }}
  hr {{ border: none; border-top: 1px dashed #cbd5e0; margin: 22px 0; }}
  .footer {{
    margin-top: 28px; padding-top: 12px; border-top: 1px solid #e2e8f0;
    font-size: 9.5pt; color: #718096;
  }}
</style>
</head>
<body>
{body}
<div class="footer">
  <strong>免责声明</strong>：本报告基于公开数据和量化信号，仅作流程演示。技术面对 1-3 天的预测可靠性有限，请结合自身风险承受能力综合决策，<strong>不构成任何投资建议</strong>。
</div>
</body>
</html>
"""


def build_markdown(report: dict, with_synthesis: bool = True) -> str:
    parts = [
        _section_header(report),
        _section_info(report),
        _section_price(report),
        _section_technical(report),
        _section_fund_flow(report),
        _section_lhb(report),
        _section_sentiment(report),
        _section_factors(report),
        _section_peers(report),
        _section_options(report),
        _section_info_events(report),
        _section_verdict(report),
    ]
    if with_synthesis:
        synth = str(report.get("synthesis") or "").strip()
        if synth:
            parts.append("---\n\n# 综合解读（LLM 增强）\n\n" + synth + "\n")
    return "\n".join(p for p in parts if p)


def render_pdf(report: dict, out_dir: str, with_synthesis: bool = True) -> str:
    # Chrome requires an absolute file URI.  ``file://output/report.html`` is
    # interpreted as a host named "output" and Chrome silently prints its
    # ERR_INVALID_URL page to an otherwise valid-looking PDF.
    output_dir = Path(out_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    code = report.get("code", "unknown")
    date = report.get("target_date", "")
    name = report.get("name", code)
    pdf_path = output_dir / f"report_{code}_{date}.pdf"

    md = build_markdown(report, with_synthesis=with_synthesis)
    body = markdown.markdown(md, extensions=["tables", "fenced_code", "nl2br"])
    html = _HTML_TEMPLATE.format(title=f"{name} {code} 分析", body=body)

    html_path = pdf_path.with_suffix(".html")
    with html_path.open("w", encoding="utf-8") as f:
        f.write(html)

    chrome = _find_chrome()
    if not chrome:
        print(f"  [PDF] Chrome/Chromium 未找到，仅生成 HTML：{html_path}")
        print("       提示：安装 Chrome 后重试，或设置 CHROME_BIN=/path/to/chrome")
        return str(html_path)

    subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path}", html_path.as_uri()],
        check=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    )
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise RuntimeError(f"Chrome 未生成 PDF：{pdf_path}")
    with pdf_path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise RuntimeError(f"Chrome 输出不是有效 PDF：{pdf_path}")
    try:
        html_path.unlink()
    except OSError:
        pass
    return str(pdf_path)


def _main_cli() -> None:
    """独立入口：从已保存的 report JSON 生成 PDF。

    供 analyze.py 以分离子进程方式异步调用：
        python report_pdf.py <report_json> <out_dir> [--no-synthesis]
    """
    args = [a for a in sys.argv[1:]]
    enrich = "--with-synthesis" in args
    with_syn = True
    if enrich:
        args = [a for a in args if a != "--with-synthesis"]
    if "--no-synthesis" in args:
        with_syn = False
        args = [a for a in args if a != "--no-synthesis"]
    if len(args) < 2:
        print("usage: report_pdf.py <report_json> <out_dir> [--with-synthesis|--no-synthesis]",
              file=sys.stderr)
        sys.exit(2)
    json_path, out_dir = args[0], args[1]
    with open(json_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    if enrich:
        report["synthesis"] = generate_synthesis(report)
    path = render_pdf(report, out_dir, with_synthesis=with_syn)
    print(path)


if __name__ == "__main__":
    _main_cli()
