"""信息储备层 — SQLite 存储 (P1)。

统一事件卡 (event card) 落盘:公告 / 年报 / 季报 / IPO / … 跨市场归一到一张表。
长文 PDF 不进库(后续落 store/raw/),库里只存元数据 + 链接 + (懒加工的)摘要。

表:
- events     : 事件卡主表,event_id 自然键去重
- sync_state : 每 (code, market, source) 的增量同步水位

P1 只做 建库 / upsert(去重) / 查询(防前视) / 同步水位读写,不接 LLM、不接信号。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

# store/ 与 scripts/ 同级,落在 skill 根目录下
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_DIR = os.path.join(_SKILL_DIR, "store")
DB_PATH = os.path.join(STORE_DIR, "info_store.db")

# 归一事件类型枚举(标准化契约,详见 info_adapters.normalize_type)
EVENT_TYPES = (
    "年报", "季报", "临时公告", "IPO", "股东大会",
    "高管交易", "调研", "投资者问答", "研报", "其他",
)


@dataclass
class EventCard:
    """统一事件卡。summary/sentiment/materiality/key_points 采集时为空,懒加工回填。"""
    event_id: str
    code: str
    market: str
    type: str
    event_date: str               # ISO YYYY-MM-DD
    name: str = ""
    subtype: str = ""             # 原始分类串(东财公告类型 / EDGAR form)
    title: str = ""
    source: str = ""              # cninfo|eastmoney|sec_edgar|akshare
    url: str = ""
    raw_path: Optional[str] = None
    summary: Optional[str] = None
    key_points: Optional[list] = None
    sentiment: Optional[float] = None
    materiality: Optional[int] = None
    raw_meta: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        d = asdict(self)
        d["key_points"] = json.dumps(self.key_points, ensure_ascii=False) if self.key_points is not None else None
        d["raw_meta"] = json.dumps(self.raw_meta, ensure_ascii=False)
        d["ingested_at"] = _now_iso()
        d["processed_at"] = None
        return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  event_id    TEXT PRIMARY KEY,
  code        TEXT NOT NULL,
  market      TEXT NOT NULL,
  name        TEXT,
  type        TEXT NOT NULL,
  subtype     TEXT,
  title       TEXT,
  event_date  TEXT NOT NULL,
  source      TEXT,
  url         TEXT,
  raw_path    TEXT,
  summary     TEXT,
  key_points  TEXT,
  sentiment   REAL,
  materiality INTEGER,
  raw_meta    TEXT,
  ingested_at TEXT NOT NULL,
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_lookup ON events(code, market, event_date);
CREATE INDEX IF NOT EXISTS idx_events_type   ON events(type, event_date);

CREATE TABLE IF NOT EXISTS sync_state (
  code TEXT, market TEXT, source TEXT,
  last_synced_date TEXT,
  last_run_at TEXT,
  PRIMARY KEY (code, market, source)
);
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


_INSERT_COLS = [
    "event_id", "code", "market", "name", "type", "subtype", "title",
    "event_date", "source", "url", "raw_path", "summary", "key_points",
    "sentiment", "materiality", "raw_meta", "ingested_at", "processed_at",
]


def upsert_events(conn: sqlite3.Connection, cards: list[EventCard]) -> tuple[int, int]:
    """INSERT OR IGNORE 去重。返回 (新增, 跳过已存在)。"""
    if not cards:
        return (0, 0)
    placeholders = ", ".join(":" + c for c in _INSERT_COLS)
    sql = f"INSERT OR IGNORE INTO events ({', '.join(_INSERT_COLS)}) VALUES ({placeholders})"
    before = conn.total_changes
    rows = [c.to_row() for c in cards]
    conn.executemany(sql, rows)
    conn.commit()
    inserted = conn.total_changes - before
    return (inserted, len(rows) - inserted)


def query_events(
    conn: sqlite3.Connection,
    code: str,
    market: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    types: Optional[list[str]] = None,
) -> list[dict]:
    """查询事件卡。end_date 即防前视上界(传 target_date 则只返回 <= target_date 的事件)。"""
    sql = "SELECT * FROM events WHERE code = ? AND market = ?"
    args: list = [code, market]
    if start_date:
        sql += " AND event_date >= ?"
        args.append(start_date)
    if end_date:
        sql += " AND event_date <= ?"
        args.append(end_date)
    if types:
        sql += f" AND type IN ({', '.join('?' * len(types))})"
        args.extend(types)
    sql += " ORDER BY event_date DESC"
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["key_points"] = json.loads(d["key_points"]) if d.get("key_points") else None
        d["raw_meta"] = json.loads(d["raw_meta"]) if d.get("raw_meta") else {}
        out.append(d)
    return out


def update_enrichment(
    conn: sqlite3.Connection,
    event_id: str,
    summary: Optional[str],
    key_points: Optional[list],
    sentiment: Optional[float],
    materiality: Optional[int],
) -> None:
    """回填 LLM 加工结果并置 processed_at(幂等:只对 processed_at 空的卡调用)。"""
    conn.execute(
        """UPDATE events SET summary=?, key_points=?, sentiment=?, materiality=?, processed_at=?
           WHERE event_id=?""",
        (
            summary,
            json.dumps(key_points, ensure_ascii=False) if key_points is not None else None,
            sentiment, materiality, _now_iso(), event_id,
        ),
    )
    conn.commit()


def get_sync_state(conn: sqlite3.Connection, code: str, market: str, source: str) -> Optional[str]:
    row = conn.execute(
        "SELECT last_synced_date FROM sync_state WHERE code=? AND market=? AND source=?",
        (code, market, source),
    ).fetchone()
    return row["last_synced_date"] if row else None


def set_sync_state(conn: sqlite3.Connection, code: str, market: str, source: str, last_synced_date: str) -> None:
    conn.execute(
        """INSERT INTO sync_state (code, market, source, last_synced_date, last_run_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(code, market, source) DO UPDATE SET
             last_synced_date=excluded.last_synced_date,
             last_run_at=excluded.last_run_at""",
        (code, market, source, last_synced_date, _now_iso()),
    )
    conn.commit()
