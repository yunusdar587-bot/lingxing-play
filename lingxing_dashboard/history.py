"""每次 15 分钟轮询的 OrderProfit 留存。独立于看板主库，只追加、不回源。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .fields import REPLACED_FIELDS
from .timeutil import local_now

STEP_MINUTES = 15
MAX_SPAN = timedelta(days=7)
_PAYLOAD_KEYS = (
    "msku", "fnsku", "asin", "afn_volume", "gross_profit", "net_amount", "selling_fee", *REPLACED_FIELDS,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    sid INTEGER NOT NULL,
    site TEXT NOT NULL,
    tz TEXT NOT NULL,
    name TEXT NOT NULL,
    local_slot TEXT NOT NULL,          -- 站点当地 YYYY-MM-DD HH:MM，向下取到 15 分钟
    taken_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT,                       -- 该刻度的接口聚合行；NULL 表示只有从旧快照补入的汇总
    PRIMARY KEY (sid, local_slot)
);
CREATE INDEX IF NOT EXISTS ix_cap_slot ON captures (local_slot);
"""


def history_db_path(db_path: str) -> str:
    if db_path == ":memory:":
        return ":memory:"
    p = Path(db_path)
    return str(p.with_name(p.stem + ".api_history.sqlite3"))


def floor_local_slot(local_dt: datetime, minutes: int = STEP_MINUTES) -> str:
    local_dt = local_dt.replace(second=0, microsecond=0)
    return local_dt.replace(minute=local_dt.minute - local_dt.minute % minutes).strftime("%Y-%m-%d %H:%M")


def parse_slot(value: str) -> datetime:
    """解析不带时区的钟面时间。分钟必须是 15 的倍数。"""
    text = str(value or "").strip().replace("T", " ")
    if len(text) == 16:
        text += ":00"
    try:
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("时间格式应为 YYYY-MM-DD HH:MM") from exc
    if dt.second or dt.minute % STEP_MINUTES:
        raise ValueError("时间要落在 15 分钟刻度上，例如 10:00、10:15")
    return dt


def slot_count(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // (STEP_MINUTES * 60)) + 1


def compact_profit(profit: dict[str, dict]) -> list[dict]:
    rows = []
    for msku, row in profit.items():
        item = {k: row.get(k) for k in _PAYLOAD_KEYS}
        item["msku"] = str(row.get("msku") or msku)
        rows.append(item)
    rows.sort(key=lambda r: r["msku"])
    return rows


def _point(summary: dict, *, local_slot: str, taken_at: str, has_payload: bool, hide: bool) -> dict:
    daily = summary.get("daily") or {}
    order = summary.get("order") or {}
    return {
        "local_slot": local_slot,
        "taken_at": taken_at,
        "has_payload": has_payload,
        "msku_count": summary.get("msku_count"),
        "afn_volume": summary.get("afn_volume"),
        "adjusted": {
            "daily": None if hide else daily.get("adjusted"),
            "order": None if hide else order.get("adjusted"),
        },
    }


class HistoryDB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)

    def save_capture(self, summary: dict, profit: dict[str, dict], taken_at_iso: str) -> str:
        tz = ZoneInfo(summary["tz"])
        slot = floor_local_slot(local_now(tz, datetime.fromisoformat(taken_at_iso)))
        payload = json.dumps(compact_profit(profit), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO captures(sid,site,tz,name,local_slot,taken_at,summary,payload) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sid,local_slot) DO UPDATE SET site=excluded.site, tz=excluded.tz, name=excluded.name, "
                "taken_at=excluded.taken_at, summary=excluded.summary, payload=excluded.payload",
                (summary["sid"], summary["site"], summary["tz"], summary["name"], slot, taken_at_iso,
                 json.dumps(summary, ensure_ascii=False), payload),
            )
        return slot

    def import_summaries(self, db) -> int:
        """把主库里已有的快照汇总补进来。已有接口明细的刻度不覆盖。"""
        added = 0
        with self._lock:
            for r in db.query("SELECT taken_at, summary FROM snapshots"):
                summary = json.loads(r["summary"])
                if not summary.get("tz"):
                    continue
                slot = floor_local_slot(local_now(ZoneInfo(summary["tz"]), datetime.fromisoformat(r["taken_at"])))
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO captures(sid,site,tz,name,local_slot,taken_at,summary,payload) "
                    "VALUES(?,?,?,?,?,?,?,NULL)",
                    (summary["sid"], summary.get("site") or "", summary["tz"], summary.get("name") or "",
                     slot, r["taken_at"], r["summary"]),
                )
                added += cur.rowcount
        return added

    def bounds(self) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT MIN(local_slot) lo, MAX(local_slot) hi, COUNT(*) n FROM captures").fetchone()
        return {"min": row["lo"], "max": row["hi"], "count": row["n"]}

    def captures_between(self, start: str, end: str) -> list[sqlite3.Row]:
        a, b = parse_slot(start), parse_slot(end)
        if b < a:
            raise ValueError("结束时刻要晚于或等于开始时刻")
        if b - a > MAX_SPAN:
            raise ValueError("一次最多拉取 7 天")
        lo, hi = a.strftime("%Y-%m-%d %H:%M"), b.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            return self._conn.execute(
                "SELECT sid, local_slot, taken_at, summary, payload IS NOT NULL AS has_payload "
                "FROM captures WHERE local_slot>=? AND local_slot<=? ORDER BY local_slot",
                (lo, hi),
            ).fetchall()

    def detail(self, sid: int, slot: str) -> dict | None:
        text = parse_slot(slot).strftime("%Y-%m-%d %H:%M")
        with self._lock:
            row = self._conn.execute(
                "SELECT sid, name, site, tz, local_slot, taken_at, summary, payload FROM captures WHERE sid=? AND local_slot=?",
                (sid, text),
            ).fetchone()
        if row is None:
            return None
        summary = json.loads(row["summary"])
        return {
            "sid": row["sid"],
            "name": row["name"],
            "site": row["site"],
            "tz": row["tz"],
            "currency": summary.get("currency"),
            "local_slot": row["local_slot"],
            "taken_at": row["taken_at"],
            "has_payload": row["payload"] is not None,
            "rows": json.loads(row["payload"]) if row["payload"] else [],
        }


def assemble(stores: list[dict], rows: list[Any], start: str, end: str, hide: bool) -> dict:
    a, b = parse_slot(start), parse_slot(end)
    lo, hi = a.strftime("%Y-%m-%d %H:%M"), b.strftime("%Y-%m-%d %H:%M")
    by_sid: dict[int, list] = {}
    for r in rows:
        summary = json.loads(r["summary"])
        by_sid.setdefault(r["sid"], []).append(_point(
            summary, local_slot=r["local_slot"], taken_at=r["taken_at"], has_payload=bool(r["has_payload"]), hide=hide,
        ))
    out_stores = []
    for s in stores:
        points = by_sid.get(s["sid"], [])
        out_stores.append({
            "sid": s["sid"], "name": s["name"], "site": s["site"], "site_name": s["site_name"], "tz": s["tz"],
            "kept": len(points), "points": points,
        })
    return {
        "start": lo, "end": hi, "step_minutes": STEP_MINUTES, "slot_count": slot_count(a, b),
        "stores": out_stores,
    }
