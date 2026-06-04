"""综合分析单只 A 股 / 港股 / ETF / 美股：技术指标 + 资金流 + LLM 情感 + 龙虎榜 → 走势预测。

用法（位置参数：code market target_date [name]）：
    A 股:  python analyze.py 603893 sh   20260522 [瑞芯微]
    港股:  python analyze.py 01810  hk   20260522 [小米集团]
    ETF :  python analyze.py 510300 etf  20260522 [沪深300ETF]
    美股:  python analyze.py AAPL   us   20260522 [Apple]

market 可选：sh / sz / hk / etf / us
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any

import akshare as ak
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from technical import compute_indicators, analyze_signals as analyze_tech
from sentiment_llm import analyze_news_with_llm
from fund_flow import fetch_with_fallback, extract_main_net
from data_layer import fetch_kline, fetch_news, fetch_info, fetch_etf_fund_flow, fetch_realtime
from report_pdf import render_pdf
from peer_scan import scan_peers, _suggest_peers
from factor import compute_factors
from options_flow import analyze_options_flow


# ---------------- 资产抽象 ----------------

@dataclass
class Asset:
    code: str
    name: str
    market: str          # 'sh' | 'sz' | 'hk' | 'etf' | 'us'
    target_date: str     # 'YYYYMMDD'

    @property
    def kind(self) -> str:
        if self.market == "hk":
            return "hk"
        if self.market == "etf":
            return "etf"
        if self.market == "us":
            return "us"
        return "astock"

    @property
    def market_for_data(self) -> str:
        """数据层用的市场前缀。ETF 自动判断 sh/sz：5/15/56/58 开头沪，1/15/16 深。"""
        if self.kind == "etf":
            return "sh" if self.code.startswith(("5", "6")) else "sz"
        return self.market

    @property
    def target_dash(self) -> str:
        d = self.target_date
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"


@dataclass
class Signal:
    name: str
    score: float
    detail: str


@dataclass
class Aggregator:
    signals: list[Signal] = field(default_factory=list)

    def add(self, name: str, score: float, detail: str):
        self.signals.append(Signal(name, score, detail))

    @property
    def total(self) -> float:
        return sum(s.score for s in self.signals)

    def show(self):
        print()
        for s in self.signals:
            mark = "↑" if s.score > 0 else ("↓" if s.score < 0 else "·")
            print(f"  [{mark} {s.score:+.2f}] {s.name}: {s.detail}")
        print(f"\n  >>> 信号合计: {self.total:+.2f}")


def section(title: str):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def safe(label: str, fn, quiet: bool = False):
    if not quiet:
        print(f"\n--- {label} ---")
    try:
        df = fn()
        if df is None or (hasattr(df, "empty") and df.empty):
            return None
        return df
    except Exception as e:
        if not quiet:
            print(f"  [失败] {type(e).__name__}: {str(e)[:120]}")
        return None


# ---------------- 信号 ----------------

def signal_price_action(kline: pd.DataFrame, asset: Asset, agg: Aggregator) -> dict:
    sub = kline[kline["date"] == asset.target_dash]
    if sub.empty:
        agg.add("当日涨跌", 0, "目标日无 K 线（停牌/非交易日）")
        return {}
    i = sub.index[0]
    row = kline.iloc[i]
    o, h, l, c, v = (float(row[k]) for k in ["open", "high", "low", "close", "volume"])
    prev_close = float(kline.iloc[i - 1]["close"]) if i > 0 else None
    pct = (c - prev_close) / prev_close * 100 if prev_close else None

    print(f"  {asset.target_dash}  开:{o:.4g}  高:{h:.4g}  低:{l:.4g}  收:{c:.4g}  量:{v:,.0f}")
    if pct is not None:
        print(f"  较前收({prev_close:.4g}) 涨跌: {pct:+.2f}%")
        if pct >= 5:
            agg.add("当日涨跌", 1.0, f"涨幅 {pct:+.2f}% 强势")
        elif pct >= 1:
            agg.add("当日涨跌", 0.5, f"涨幅 {pct:+.2f}% 小阳")
        elif pct <= -5:
            agg.add("当日涨跌", -1.0, f"跌幅 {pct:+.2f}% 重挫")
        elif pct <= -1:
            agg.add("当日涨跌", -0.5, f"跌幅 {pct:+.2f}% 小阴")
        else:
            agg.add("当日涨跌", 0, f"窄幅 {pct:+.2f}%")
    return {"open": o, "high": h, "low": l, "close": c, "volume": v,
            "prev_close": prev_close, "pct_change": pct}


def signal_technical(kline: pd.DataFrame, asset: Asset, agg: Aggregator) -> dict:
    df = compute_indicators(kline)
    res = analyze_tech(df, asset.target_dash)
    weighted = res["score"] * 0.7
    agg.add(
        "技术指标(MA/RSI/BOLL/MACD)",
        weighted,
        f"原始 {res['score']:+.2f} ×0.7 (" +
        "; ".join(s.split(' → ')[0] for s in res["signals"][:4]) + ")"
    )
    print()
    for s in res["signals"]:
        print(f"    · {s}")
    return res


def signal_fund_flow(asset: Asset, agg: Aggregator) -> dict | None:
    """A 股: 三源回退；ETF: fund_etf_spot_em；港股 / 美股: 跳过（无个股口径）。"""
    if asset.kind == "hk":
        agg.add("资金流", 0, "港股无个股资金流口径，跳过")
        return None
    if asset.kind == "us":
        agg.add("资金流", 0, "美股无个股资金流口径，跳过")
        return None

    if asset.kind == "etf":
        v = fetch_etf_fund_flow(asset.code, asset.target_dash)
        if v is None:
            agg.add("资金流", 0, "ETF 资金流快照不在目标日（仅 spot 接口可用）")
            return None
        yi = v / 1e8
        detail = f"主力净额 {yi:+.2f} 亿元 (东财 ETF spot)"
        if v > 1e8:    agg.add("资金流", 1.0, detail)
        elif v > 1e7:  agg.add("资金流", 0.5, detail)
        elif v < -1e8: agg.add("资金流", -1.0, detail)
        elif v < -1e7: agg.add("资金流", -0.5, detail)
        else:          agg.add("资金流", 0, detail)
        return {"main_net_yuan": v, "source": "etf_spot_em"}

    # A 股
    df, source = fetch_with_fallback(asset.code, asset.market_for_data)
    if df is None:
        agg.add("资金流", 0, "全部数据源失败")
        return None
    print(f"  ✓ 来源: {source}  rows={len(df)}")
    main_net = extract_main_net(df, source, asset.target_dash)
    if main_net is None:
        agg.add("资金流", 0, f"未匹配目标日（{source}）")
        return {"source": source}
    yi = main_net / 1e8
    detail = f"主力净额 {yi:+.2f} 亿元 ({source})"
    if main_net > 1e8:    agg.add("资金流", 1.0, detail)
    elif main_net > 1e7:  agg.add("资金流", 0.5, detail)
    elif main_net < -1e8: agg.add("资金流", -1.0, detail)
    elif main_net < -1e7: agg.add("资金流", -0.5, detail)
    else:                 agg.add("资金流", 0, detail)
    return {"main_net_yuan": main_net, "source": source}


def signal_lhb(asset: Asset, agg: Aggregator) -> dict | None:
    """龙虎榜仅适用于 A 股个股。"""
    if asset.kind != "astock":
        return None
    lhb = safe(
        "stock_lhb_detail_em",
        lambda: ak.stock_lhb_detail_em(start_date=asset.target_date, end_date=asset.target_date),
        quiet=True,
    )
    if lhb is None or lhb.empty:
        agg.add("龙虎榜", 0, "当日无龙虎榜")
        return None
    sub = lhb[lhb["代码"].astype(str) == asset.code]
    if sub.empty:
        agg.add("龙虎榜", 0, f"未上榜 (当日 {len(lhb)} 只)")
        return None
    print(sub.to_string(index=False))
    agg.add("龙虎榜", 0, "上榜 → 资金博弈剧烈，方向中性但波动放大")
    return sub.to_dict("records")


def signal_sentiment(asset: Asset, agg: Aggregator) -> dict:
    """新闻情感分析。ETF 跳过；A 股 / 港股 / 美股 都跑。"""
    if asset.kind == "etf":
        agg.add("LLM情感", 0, "ETF 无个股新闻，跳过")
        return {}
    news = fetch_news(asset.kind, asset.code, name=asset.name)
    if news is None or news.empty:
        agg.add("LLM情感", 0, "无新闻")
        return {}
    print(f"  原始新闻 {len(news)} 条 → 送入 Claude 分析（去重 + 重要性分级）")
    res = analyze_news_with_llm(
        news, f"{asset.name} {asset.code}", asset.target_dash, market=asset.kind
    )
    if "error" in res:
        agg.add("LLM情感", 0, f"分析失败: {res['error']}")
        return res

    score = float(res.get("score", 0))
    verdict = res.get("verdict", "neutral")
    confidence = res.get("confidence", "low")
    rationale = res.get("rationale", "")
    n_events = len(res.get("events", []))
    conf_mult = {"low": 0.5, "medium": 0.75, "high": 1.0}.get(confidence, 0.5)
    weighted = (score / 5) * conf_mult

    agg.add("LLM情感", weighted,
            f"verdict={verdict}({confidence}) 原始 {score:+.1f}/5  事件 {n_events} 条 — {rationale[:80]}")
    print(f"\n  事件清单 ({n_events})：")
    for e in res.get("events", []):
        print(f"    [{e.get('direction','?')}{e.get('importance','?')}/{e.get('timing','?')}] "
              f"{e.get('summary','')[:60]} (源数 {e.get('sources_count', 1)})")
    return res


def signal_factor(kline: pd.DataFrame, asset: Asset, peers: dict | None,
                  agg: Aggregator, validate: bool = True) -> dict:
    """4 类因子打分 → 有效性检验 → 调整权重 → weighted_score ×0.8 进总信号。"""
    res = compute_factors(
        kline=kline, code=asset.code, market=asset.kind,
        target_dash=asset.target_dash, peers=peers, validate=validate,
    )
    cat_scores = res.get("category_scores") or {}
    weighted = float(res.get("weighted_score", 0.0))
    agg_score = weighted * 0.8

    pretty = " | ".join(f"{k}={v:+.2f}" for k, v in cat_scores.items()) or "无类别得分"
    agg.add(
        "多因子合成",
        agg_score,
        f"加权 {weighted:+.2f} ×0.8 ({pretty})",
    )

    # === 因子有效性表 ===
    val = res.get("validation") or {}
    summary = val.get("summary") or {}
    if validate and val.get("results"):
        print()
        grades = summary.get("grades") or {}
        print(f"  [有效性检验] |IC|≥0.06 + |IR|≥0.5 通过；"
              f"评级 A:{grades.get('A',0)} B:{grades.get('B',0)} "
              f"C:{grades.get('C',0)} D:{grades.get('D',0)} "
              f"({summary.get('passed',0)}/{summary.get('total',0)} 通过, "
              f"平均 |IC|={summary.get('mean_abs_IC')}, |IR|={summary.get('mean_abs_IR')})")

    # === 各类别详情（标注每个因子的评级和权重）===
    print()
    cat_zh = {"technical": "技术", "style": "风格", "fundamental": "基本面",
              "relative_strength": "相对强度"}
    for cat_key, cat_data in res.get("categories", {}).items():
        zh = cat_zh.get(cat_key, cat_key)
        if isinstance(cat_data, dict) and cat_data.get("error"):
            print(f"  · {zh}: 跳过（{cat_data['error']}）")
            continue
        if not isinstance(cat_data, dict) or "items" not in cat_data:
            continue
        cs = cat_data.get("category_score", 0)
        mark = "↑" if cs > 0.1 else ("↓" if cs < -0.1 else "·")
        kept = cat_data.get("n_items_kept", len(cat_data["items"]))
        total = cat_data.get("n_items_total", len(cat_data["items"]))
        print(f"  [{mark} {cs:+.2f}] {zh}因子（保留 {kept}/{total} 项）：")
        for it in cat_data["items"]:
            mult = it.get("weight_multiplier", 1.0)
            v = it.get("validation") or {}
            grade = v.get("grade") if v else None
            if mult == 0:
                badge = "✗D"
                show_score = it.get("raw_score", 0)
                tail = " 【剔除】"
            elif grade in ("A", "B"):
                badge = f"✓{grade}"
                show_score = it["score"]
                ic = v.get("IC"); ir = v.get("IR")
                tail = f"  IC={ic:+.3f} IR={ir:+.2f}" if ic is not None else ""
            elif grade == "C":
                badge = "△C"
                show_score = it["score"]
                ic = v.get("IC"); ir = v.get("IR")
                tail = f"  IC={ic:+.3f} IR={ir:+.2f} (×0.5)" if ic is not None else " (×0.5)"
            elif grade == "?":
                badge = " ?"
                show_score = it["score"]
                tail = f"  (样本不足)"
            else:
                # 静态因子（无 validation_key）
                badge = " ·"
                show_score = it["score"]
                tail = "  [静态]"
            print(f"      [{badge} {show_score:+.2f}] {it['name']}: "
                  f"{it['value']} — {it['remark']}{tail}")
    return res


def signal_options_flow(asset: Asset, agg: Aggregator) -> dict:
    """期权流：A 股 ETF/指数期权看 PCR + QVIX；美股看 yfinance 期权链。

    A 股个股 / 港股本身无可交易期权，标记 available=False 并以 0 分计入。"""
    res = analyze_options_flow(asset.kind, asset.code, asset.market_for_data,
                               asset.target_dash)
    detail = res.get("detail", "")
    score = float(res.get("score") or 0.0)
    if res.get("available"):
        name = res.get("underlying_name") or res.get("underlying") or asset.code
        print(f"  标的: {res.get('underlying')} {name}（{res.get('source','')}）")
        print(f"  认购量 {res.get('call_vol'):,.0f} / 认沽量 {res.get('put_vol'):,.0f}"
              f"　|　未平仓 认购 {res.get('call_oi'):,.0f} / 认沽 {res.get('put_oi'):,.0f}")
        if res.get("iv") is not None:
            pct = res.get("iv_percentile_1y")
            pct_s = f"，1 年分位 {pct*100:.0f}%" if pct is not None else ""
            print(f"  隐含波动率 {res.get('iv')}{pct_s}")
    else:
        print(f"  {detail}")
    agg.add("期权流(PCR/IV)", score, detail)
    return res


# ---------------- 预测 ----------------

def make_verdict(total_score: float) -> tuple[str, str]:
    if total_score >= 2: return "看多 ↑↑", "中高"
    if total_score >= 1: return "偏多 ↗", "中"
    if total_score >= 0.3: return "弱偏多 →↗", "低"
    if total_score <= -2: return "看空 ↓↓", "中高"
    if total_score <= -1: return "偏空 ↘", "中"
    if total_score <= -0.3: return "弱偏空 →↘", "低"
    return "震荡 →", "低"


def next_open_date(target: str) -> str:
    d = datetime.strptime(target, "%Y%m%d")
    add = 3 if d.weekday() == 4 else 1
    return (d + timedelta(days=add)).strftime("%Y-%m-%d (%a)")


def kind_label(kind: str) -> str:
    return {"astock": "A 股", "hk": "港股", "etf": "ETF", "us": "美股"}.get(kind, kind)


# ---------------- 主流程 ----------------

def _print_peers_summary(peers: dict) -> None:
    industry_zh = peers.get("industry_zh") or "—"
    industry_en = peers.get("industry_en") or ""
    stats = peers.get("stats") or {}
    print(f"\n  行业: {industry_zh}" + (f" / {industry_en}" if industry_en else ""))
    print(f"  筛选: A 股 {stats.get('a_kept',0)}/{stats.get('a_total',0)}, "
          f"美股 {stats.get('us_kept',0)}/{stats.get('us_total',0)}（看涨且置信度≥中）")

    a_list = peers.get("a_stock") or []
    us_list = peers.get("us_stock") or []
    if a_list:
        print(f"\n  >>> A 股板块（按信号分降序，top {len(a_list)}）：")
        for r in a_list:
            print(f"    · {r['code']} {r.get('name','')}  {r['brief']}")
            if r.get("reason"):
                print(f"        ↳ {r['reason']}")
    else:
        print("\n  >>> A 股板块：无符合（看涨且置信度≥中）的标的")

    if us_list:
        print(f"\n  >>> 美股板块（按信号分降序，top {len(us_list)}）：")
        for r in us_list:
            ticker = r.get("ticker") or r.get("code", "")
            print(f"    · {ticker} {r.get('name','')}  {r['brief']}")
            if r.get("reason"):
                print(f"        ↳ {r['reason']}")
    else:
        print("\n  >>> 美股板块：无符合（看涨且置信度≥中）的标的")


def run(asset: Asset, out_dir: str, skip_peers: bool = False,
        validate_factors: bool = True, async_pdf: bool = True) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    report: dict[str, Any] = {
        "code": asset.code, "name": asset.name, "market": asset.market,
        "kind": asset.kind, "target_date": asset.target_date,
    }
    agg = Aggregator()

    section(f"1. 基本信息 [{kind_label(asset.kind)}] — {asset.name} {asset.code}")
    info = fetch_info(asset.kind, asset.code, asset.market_for_data)
    if info is not None and info.shape[1] >= 2:
        kv = dict(zip(info.iloc[:, 0], info.iloc[:, 1]))
        report["info"] = kv
        for k, v in list(kv.items())[:6]:
            if v is not None and str(v).strip():
                print(f"  {k}: {str(v)[:80]}")

    section(f"2. K 线 + 当日价格 ({asset.target_dash})")
    # 拉 500 天（约 2 年）K 线 — 足够支撑 60 日滚动 IC + 5 日 fwd return 的因子有效性检验
    kline = fetch_kline(asset.kind, asset.code, asset.market_for_data,
                        asset.target_date, lookback=500)
    if kline is None or kline.empty:
        print("❌ 无法获取 K 线，分析终止")
        report["error"] = "no_kline"
        return report
    print(f"  取到 {len(kline)} 个交易日；最近 5 日：")
    print(kline.tail(5)[["date", "open", "high", "low", "close", "volume"]].to_string(index=False))
    report["price"] = signal_price_action(kline, asset, agg)
    report["recent_kline"] = kline.tail(6)[["date", "open", "high", "low", "close", "volume"]].astype(
        {"date": str}
    ).to_dict("records")

    # itick 实时报价：仅当分析目标为今日时附带盘中最新价（display-only，不参与因子计算，避免前视偏差）
    if asset.target_dash == datetime.now().strftime("%Y-%m-%d"):
        rt = fetch_realtime(asset.kind, asset.code, asset.market_for_data)
        if rt and rt.get("price") is not None:
            rt["queried_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 提问时间点
            chp = rt.get("change_pct")
            chp_s = f"{chp:+.2f}%" if isinstance(chp, (int, float)) else "—"
            vol = rt.get("volume") or 0
            print(f"  ⚡ itick 实时: {rt['price']} ({chp_s})  量 {vol:,}  [{rt['region']}]  截至 {rt['queried_at']}")
            report["realtime"] = rt

    section("3. 技术指标")
    tech = signal_technical(kline, asset, agg)
    report["technical"] = {"score_raw": tech["score"], "snapshot": tech["snapshot"], "rules": tech["signals"]}

    section("4. 资金流")
    report["fund_flow"] = signal_fund_flow(asset, agg)

    if asset.kind == "astock":
        section(f"5. 龙虎榜 ({asset.target_date})")
        report["lhb"] = signal_lhb(asset, agg)

    # peer 推荐(LLM) 与新闻情感(LLM) 互不依赖 → 并发发起，让两次 Opus 往返重叠
    run_peers = asset.kind != "etf" and not skip_peers
    peer_executor = None
    peer_rec_future = None
    if run_peers:
        info_dict = report.get("info") or {}
        industry_hint = (info_dict.get("行业")
                         or info_dict.get("所处行业")
                         or info_dict.get("Sector")
                         or info_dict.get("Industry"))
        peer_executor = ThreadPoolExecutor(max_workers=1)
        peer_rec_future = peer_executor.submit(
            _suggest_peers, asset.name, asset.code, asset.kind, industry_hint)

    section("6. 新闻情感（Claude API）")
    report["sentiment"] = signal_sentiment(asset, agg)

    # 板块扫描提前到因子前，因子的"相对强度"会复用 peers 数据
    peers_data: dict | None = None
    if run_peers:
        section("7. 板块联动 — 同行业看多标的")
        try:
            rec = peer_rec_future.result()  # 多半已在情感分析期间跑完
            peers_data = scan_peers(
                target_name=asset.name, target_code=asset.code,
                target_market=asset.kind, target_date=asset.target_date,
                top_n=5, prefetched_rec=rec,
            )
            report["peers"] = peers_data
            if peers_data and "error" not in peers_data:
                _print_peers_summary(peers_data)
        except Exception as e:
            print(f"  [Peer] 扫描失败: {e}")
        finally:
            if peer_executor is not None:
                peer_executor.shutdown(wait=False)

    section("8. 多因子打分卡（风格 / 技术 / 基本面 / 相对强度，含 IC/IR 有效性检验）")
    try:
        report["factors"] = signal_factor(
            kline, asset, peers_data, agg,
            validate=validate_factors,
        )
    except Exception as e:
        print(f"  [因子] 失败: {type(e).__name__}: {str(e)[:120]}")
        report["factors"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}

    section("9. 期权流（PCR / 隐含波动率）")
    report["options_flow"] = signal_options_flow(asset, agg)

    section("10. 综合判断")
    agg.show()
    verdict, conf = make_verdict(agg.total)
    nxt = next_open_date(asset.target_date)
    print(f"\n  >>> 下一交易日 {nxt} 开盘倾向: {verdict}（置信度 {conf}）")
    report["verdict"] = {
        "score_total": agg.total,
        "verdict": verdict,
        "confidence": conf,
        "next_open": nxt,
        "signals": [asdict(s) for s in agg.signals],
    }

    out = os.path.join(out_dir, f"report_{asset.code}_{asset.target_date}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  JSON → {out}")

    section("11. 生成 PDF 报告")
    pdf_path = os.path.join(out_dir, f"report_{asset.code}_{asset.target_date}.pdf")
    if async_pdf:
        # 综述 LLM(~10-15s) + Chrome 渲染移到分离子进程，主流程不阻塞、立即返回结果。
        # 子进程从已存的 JSON 读 report，独立生成；start_new_session 让它脱离父进程存活。
        log_path = pdf_path.replace(".pdf", ".pdflog")
        try:
            logf = open(log_path, "wb")
            subprocess.Popen(
                [sys.executable, os.path.join(HERE, "report_pdf.py"), out, out_dir],
                stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
            )
            print(f"  PDF 后台生成中（含综述 LLM）→ {pdf_path}")
            print(f"     完成前请稍候几秒；如未生成可看日志 {log_path}")
        except Exception as e:
            print(f"  [PDF 异步启动失败，回退同步]: {e}")
            try:
                print(f"  PDF  → {render_pdf(report, out_dir, with_synthesis=True)}")
            except Exception as e2:
                print(f"  [PDF 生成失败]: {e2}")
    else:
        try:
            print(f"  PDF  → {render_pdf(report, out_dir, with_synthesis=True)}")
        except Exception as e:
            print(f"  [PDF 生成失败]: {e}")

    print("\n⚠️  仅作流程演示，不构成投资建议。")
    return report


def parse_args():
    p = argparse.ArgumentParser(description="个股/港股/ETF 综合分析")
    p.add_argument("code", help="代码：A 股 6 位 / 港股 5 位 / ETF 6 位 / 美股字母代码")
    p.add_argument("market", choices=["sh", "sz", "hk", "etf", "us"],
                   help="sh=沪 / sz=深 / hk=港股 / etf=ETF / us=美股")
    p.add_argument("target_date", help="目标交易日 YYYYMMDD")
    p.add_argument("name", nargs="?", default="", help="名称（可选）")
    p.add_argument("--out", default=None, help="输出目录，默认 ./output")
    p.add_argument("--no-peers", action="store_true",
                   help="不扫描同行业 peers（默认会跑，跑一次约 +20-60s）")
    p.add_argument("--no-validate-factors", action="store_true",
                   help="跳过因子有效性 IC/IR 检验，所有因子全权计入")
    p.add_argument("--sync-pdf", action="store_true",
                   help="同步生成 PDF（阻塞等综述 LLM + Chrome）；默认异步后台生成")
    return p.parse_args()


def main():
    args = parse_args()
    asset = Asset(code=args.code, name=args.name or args.code,
                  market=args.market, target_date=args.target_date)
    out_dir = args.out or os.path.join(os.getcwd(), "output")
    run(asset, out_dir,
        skip_peers=args.no_peers,
        validate_factors=not args.no_validate_factors,
        async_pdf=not args.sync_pdf)


if __name__ == "__main__":
    main()
