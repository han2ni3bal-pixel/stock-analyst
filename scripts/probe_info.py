"""信息储备层 — P1 验收 CLI。

用法:
    probe_info.py <code> <market> <target_date> [name]
    probe_info.py 603893 sh 20260608 瑞芯微
    probe_info.py AAPL   us 20260608 Apple

流程:增量同步(按 sync_state 水位)→ 落库去重 → 查询 event_date<=target_date(防前视)→ 打印。
P1 不接 LLM、不接信号、不改 analyze.py。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime

import info_store as store
from info_adapters import sync_events

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("probe_info")


def _to_iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 1
    code, market, target_date = argv[0], argv[1], _to_iso(argv[2])
    name = argv[3] if len(argv) > 3 else ""

    conn = store.connect()
    store.init_db(conn)

    print(f"\n=== 同步 {code} ({market}) ===")
    sync_events(conn, code, market, name)

    rows = store.query_events(conn, code, market, end_date=target_date)
    print(f"\n=== 事件卡(event_date <= {target_date},防前视) 共 {len(rows)} 条 ===")
    print(f"{'日期':<11} {'类型':<6} {'原始':<12} 标题")
    print("-" * 90)
    for r in rows[:40]:
        title = (r["title"] or "")[:40]
        print(f"{r['event_date']:<11} {r['type']:<6} {(r['subtype'] or '')[:12]:<12} {title}")
    if len(rows) > 40:
        print(f"… 其余 {len(rows) - 40} 条略")

    # 防前视自检:库里是否有 > target_date 的事件没被查出来
    all_rows = store.query_events(conn, code, market)
    future = [r for r in all_rows if r["event_date"] > target_date]
    print(f"\n[自检] 库内总计 {len(all_rows)} 条;其中晚于 target 的 {len(future)} 条已被防前视过滤掉(不应出现在上表)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
