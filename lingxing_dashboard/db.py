from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
    sid INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    site TEXT NOT NULL,
    raw TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 今日最新 OrderProfit（覆盖写入，禁止累加）
CREATE TABLE IF NOT EXISTS profit_latest (
    sid INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    msku TEXT NOT NULL,
    row TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (sid, local_date, msku)
);

CREATE TABLE IF NOT EXISTS poll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sid INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    poll_run_id INTEGER,
    summary TEXT NOT NULL,
    rows TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots (sid, local_date, id);

CREATE TABLE IF NOT EXISTS job_status (
    sid INTEGER NOT NULL,
    task TEXT NOT NULL,
    period TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (sid, task, period)
);

CREATE TABLE IF NOT EXISTS return_rate_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sid INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS return_rates (
    version_id INTEGER NOT NULL,
    sid INTEGER NOT NULL,
    msku TEXT NOT NULL,
    q REAL NOT NULL,
    raw TEXT,
    flagged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, msku)
);

-- 月仓储费原始行，唯一键 sid + fnsku + 收费月 + 仓库
CREATE TABLE IF NOT EXISTS storage_fee_rows (
    sid INTEGER NOT NULL,
    fnsku TEXT NOT NULL,
    month TEXT NOT NULL,
    fulfillment_center TEXT NOT NULL,
    row TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (sid, fnsku, month, fulfillment_center)
);

CREATE TABLE IF NOT EXISTS listing_map (
    sid INTEGER NOT NULL,
    fnsku TEXT NOT NULL,
    msku TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    dimension TEXT,                  -- Listing dimension_info，仓储报告没有体积时用
    PRIMARY KEY (sid, fnsku)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    month TEXT NOT NULL,
    code TEXT NOT NULL,
    my_rate REAL,
    rate_org REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (month, code)
);

CREATE TABLE IF NOT EXISTS age_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sid INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    snapshot_date TEXT,
    rows TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_age ON age_snapshots (sid, id);

CREATE TABLE IF NOT EXISTS fifo_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sid INTEGER NOT NULL,
    msku TEXT NOT NULL,
    kind TEXT NOT NULL,              -- opening | received
    receive_date TEXT,               -- 开账批次为 NULL（入仓日未知）
    qty_in INTEGER NOT NULL,
    qty_remaining INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lots ON fifo_lots (sid, msku);

CREATE TABLE IF NOT EXISTS fifo_daily (
    sid INTEGER NOT NULL,
    msku TEXT NOT NULL,
    local_date TEXT NOT NULL,
    consumed INTEGER NOT NULL,
    last_seen_volume INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (sid, msku, local_date)
);

CREATE TABLE IF NOT EXISTS fifo_consumption (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sid INTEGER NOT NULL,
    msku TEXT NOT NULL,
    local_date TEXT NOT NULL,
    lot_id INTEGER,                  -- NULL：队列已空，来源未知
    qty INTEGER NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cons ON fifo_consumption (sid, local_date, msku);

CREATE TABLE IF NOT EXISTS received_seen (
    sid INTEGER NOT NULL,
    row_key TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (sid, row_key)
);

CREATE TABLE IF NOT EXISTS manual_return_rates (
    sid INTEGER NOT NULL,
    msku TEXT NOT NULL,
    q REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (sid, msku)
);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    sid INTEGER,
    msku TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._shared: sqlite3.Connection | None = None
        if path == ":memory:":
            self._shared = self._open()
        with self._write_lock:
            conn = self.conn()
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(listing_map)")}
        if "dimension" not in cols:
            conn.execute("ALTER TABLE listing_map ADD COLUMN dimension TEXT")

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._open()
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        if self._shared is not None:
            with self._write_lock:
                return self.conn().execute(sql, params).fetchall()
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- kv ----
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any, conn: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        payload = json.dumps(value, ensure_ascii=False)
        if conn is not None:
            conn.execute(sql, (key, payload))
        else:
            with self.tx() as c:
                c.execute(sql, (key, payload))

    def anomaly(self, at: str, sid: int | None, msku: str | None, kind: str, message: str,
                conn: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO anomalies(at,sid,msku,kind,message) VALUES(?,?,?,?,?)"
        if conn is not None:
            conn.execute(sql, (at, sid, msku, kind, message))
        else:
            with self.tx() as c:
                c.execute(sql, (at, sid, msku, kind, message))
