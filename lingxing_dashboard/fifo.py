"""先入先出队列：开账未知批次在前，其后按签收日从早到晚；销售从队首消化。

每日库龄报告不得改写这条队列。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from .db import Database
from .timeutil import iso

ORDER_SQL = (
    "SELECT * FROM fifo_lots WHERE sid=? AND msku=? AND qty_remaining>0 "
    "ORDER BY CASE kind WHEN 'opening' THEN 0 ELSE 1 END, receive_date, id"
)


def t0_key(sid: int) -> str:
    return f"fifo_t0:{sid}"


def open_books(db: Database, sid: int, t0: datetime, opening_qty: dict[str, int]) -> int:
    """开账：每个 msku 一笔入仓日未知的批次。已开账则不重复。返回建立的批次数。"""
    with db.tx() as c:
        if c.execute("SELECT 1 FROM kv WHERE key=?", (t0_key(sid),)).fetchone():
            return 0
        n = 0
        for msku, qty in sorted(opening_qty.items()):
            q = int(round(qty))
            if q <= 0:
                continue
            c.execute(
                "INSERT INTO fifo_lots(sid,msku,kind,receive_date,qty_in,qty_remaining,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (sid, msku, "opening", None, q, q, iso(t0)),
            )
            n += 1
        db.set(t0_key(sid), iso(t0), conn=c)
        return n


def receipt_key(row: dict) -> str:
    parts = [str(row.get(k) or "") for k in
             ("received_date_report", "received_date", "fnsku", "sku", "msku", "fba_shipment_id",
              "fulfillment_center_id", "quantity", "detailed_disposition")]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def new_receipts(c: sqlite3.Connection, sid: int, rows: list[dict]) -> list[dict]:
    """同样内容的签收行按出现次数去重，重复拉取同一天只入账新增的部分。"""
    counts = Counter(receipt_key(r) for r in rows)
    first: dict[str, dict] = {}
    for r in rows:
        first.setdefault(receipt_key(r), r)
    fresh: list[dict] = []
    for key, cnt in counts.items():
        seen = c.execute("SELECT count FROM received_seen WHERE sid=? AND row_key=?", (sid, key)).fetchone()
        already = seen["count"] if seen else 0
        if cnt > already:
            fresh.extend([first[key]] * (cnt - already))
            c.execute(
                "INSERT INTO received_seen(sid,row_key,count) VALUES(?,?,?) "
                "ON CONFLICT(sid,row_key) DO UPDATE SET count=excluded.count",
                (sid, key, cnt),
            )
    return fresh


def apply_receipt(db: Database, c: sqlite3.Connection, sid: int, msku: str, receive_date: date, qty: int,
                  now: datetime) -> None:
    if qty > 0:
        c.execute(
            "INSERT INTO fifo_lots(sid,msku,kind,receive_date,qty_in,qty_remaining,created_at) VALUES(?,?,?,?,?,?,?)",
            (sid, msku, "received", receive_date.isoformat(), qty, qty, iso(now)),
        )
        return
    if qty == 0:
        return
    # 负数：从最新的已知批次往前冲减，不动开账批次
    need = -qty
    lots = c.execute(
        "SELECT id, qty_remaining FROM fifo_lots WHERE sid=? AND msku=? AND kind='received' AND qty_remaining>0 "
        "ORDER BY receive_date DESC, id DESC",
        (sid, msku),
    ).fetchall()
    for lot in lots:
        if need <= 0:
            break
        take = min(need, lot["qty_remaining"])
        c.execute("UPDATE fifo_lots SET qty_remaining=qty_remaining-? WHERE id=?", (take, lot["id"]))
        need -= take
    if need > 0:
        db.anomaly(iso(now), sid, msku, "negative_receipt",
                   f"签收冲减 {-qty} 件，已知批次只够冲减 {-qty - need} 件，剩余 {need} 件未冲减（不改开账批次）",
                   conn=c)


@dataclass
class ConsumeResult:
    increment: int
    consumed_today: int
    anomaly: str | None = None


def consume(db: Database, c: sqlite3.Connection, sid: int, msku: str, local_date: date, afn_volume: int,
            observed_at: datetime) -> ConsumeResult:
    """本次 afn_volume 减去本店今天已消化件数得到增量，增量从队首消化。"""
    ld = local_date.isoformat()
    row = c.execute("SELECT consumed FROM fifo_daily WHERE sid=? AND msku=? AND local_date=?",
                    (sid, msku, ld)).fetchone()
    consumed = row["consumed"] if row else 0
    inc = afn_volume - consumed
    note = None
    if inc < 0:
        note = f"afn_volume {afn_volume} 小于今天已消化 {consumed}，不回退队列"
        db.anomaly(iso(observed_at), sid, msku, "negative_increment", note, conn=c)
    elif inc > 0:
        need = inc
        for lot in c.execute(ORDER_SQL, (sid, msku)).fetchall():
            if need <= 0:
                break
            take = min(need, lot["qty_remaining"])
            c.execute("UPDATE fifo_lots SET qty_remaining=qty_remaining-? WHERE id=?", (take, lot["id"]))
            c.execute(
                "INSERT INTO fifo_consumption(sid,msku,local_date,lot_id,qty,observed_at) VALUES(?,?,?,?,?,?)",
                (sid, msku, ld, lot["id"], take, iso(observed_at)),
            )
            need -= take
        if need > 0:
            c.execute(
                "INSERT INTO fifo_consumption(sid,msku,local_date,lot_id,qty,observed_at) VALUES(?,?,?,?,?,?)",
                (sid, msku, ld, None, need, iso(observed_at)),
            )
            note = f"队列不足，{need} 件销量无批次可消化（来源未知）"
            db.anomaly(iso(observed_at), sid, msku, "queue_exhausted", note, conn=c)
        consumed += inc
    c.execute(
        "INSERT INTO fifo_daily(sid,msku,local_date,consumed,last_seen_volume,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(sid,msku,local_date) DO UPDATE SET consumed=excluded.consumed, "
        "last_seen_volume=excluded.last_seen_volume, updated_at=excluded.updated_at",
        (sid, msku, ld, consumed, afn_volume, iso(observed_at)),
    )
    return ConsumeResult(max(inc, 0), consumed, note)


@dataclass
class SoldPiece:
    qty: int
    kind: str                 # opening | received | unknown
    receive_date: date | None
    observed_at: str


def sold_today(db: Database, sid: int, local_date: date) -> dict[str, list[SoldPiece]]:
    out: dict[str, list[SoldPiece]] = {}
    rows = db.query(
        "SELECT c.msku, c.qty, c.observed_at, l.kind, l.receive_date FROM fifo_consumption c "
        "LEFT JOIN fifo_lots l ON l.id=c.lot_id WHERE c.sid=? AND c.local_date=? ORDER BY c.id",
        (sid, local_date.isoformat()),
    )
    for r in rows:
        kind = r["kind"] or "unknown"
        rd = date.fromisoformat(r["receive_date"]) if r["receive_date"] else None
        out.setdefault(r["msku"], []).append(SoldPiece(r["qty"], kind, rd, r["observed_at"]))
    return out


def queue_state(db: Database, sid: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in db.query(
        "SELECT msku, kind, SUM(qty_remaining) q FROM fifo_lots WHERE sid=? GROUP BY msku, kind", (sid,)
    ):
        out.setdefault(r["msku"], {"opening": 0, "received": 0})[r["kind"]] = int(r["q"] or 0)
    return out


def dump_queue(db: Database, sid: int, msku: str) -> str:
    rows = db.query(ORDER_SQL, (sid, msku))
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)
