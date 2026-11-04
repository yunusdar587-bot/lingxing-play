"""三个调度各自的任务：月任务、日任务（含开账）、15 分钟轮询。互不混用。"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Callable

from . import fifo, pricing
from .calc import StoreContext, compute_rows, sign_check, summarize
from .config import Config
from .db import Database
from .fields import aggregate, normalize_row, num, parse_return_rate
from .lingxing import LingxingApi
from .refdata import fx_months_to_try, fx_rate, load_age_snapshot, load_dims, load_return_rates, snapshot_date_of
from .sites import SITES, Site, site_for_seller
from .timeutil import iso, local_now, month_str, parse_date, parse_local_datetime, prev_month, utcnow

log = logging.getLogger(__name__)

TASK_LISTING = "listing"
TASK_STORAGE_MONTH = "storage_month"
TASK_RETURN_RATE = "return_rate"
TASK_DAILY = "daily"

Q_LAG_DAYS = 30
Q_WINDOW_START_DAYS = 395


class Engine:
    def __init__(self, cfg: Config, db: Database, api: LingxingApi, clock: Callable[[], datetime] = utcnow):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.clock = clock

    # ------------------------------------------------------------------ 工具
    def _status(self, sid: int, task: str, period: str):
        return self.db.one("SELECT * FROM job_status WHERE sid=? AND task=? AND period=?", (sid, task, period))

    def _set_status(self, sid: int, task: str, period: str, status: str, message: str = "") -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO job_status(sid,task,period,status,attempts,message,updated_at) VALUES(?,?,?,?,1,?,?) "
                "ON CONFLICT(sid,task,period) DO UPDATE SET status=excluded.status, attempts=job_status.attempts+1, "
                "message=excluded.message, updated_at=excluded.updated_at",
                (sid, task, period, status, message[:2000], iso(self.clock())),
            )

    def stores(self) -> list[dict]:
        out = []
        for r in self.db.query("SELECT sid, name, site FROM stores ORDER BY site, name"):
            out.append({"sid": r["sid"], "name": r["name"], "site": SITES[r["site"]]})
        return out

    def store(self, sid: int) -> dict | None:
        r = self.db.one("SELECT sid, name, site FROM stores WHERE sid=?", (sid,))
        return {"sid": r["sid"], "name": r["name"], "site": SITES[r["site"]]} if r else None

    def local_today(self, site: Site, now: datetime | None = None) -> date:
        return local_now(site.tz, now or self.clock()).date()

    def active_price(self) -> dict:
        state = self.db.get("price_active")
        version = state["version"] if state else pricing.bootstrap_version()
        table = pricing.get_version(version)
        if table is None:
            table = pricing.get_version(pricing.bootstrap_version())
        return table  # type: ignore[return-value]

    def price_state(self) -> dict:
        state = self.db.get("price_active") or {"version": pricing.bootstrap_version(), "confirmed_at": None,
                                                "bootstrap": True}
        active = self.active_price()
        newer = [
            {"version": t["version"], "effective_date": t["effective_date"], "notes": t.get("notes", "")}
            for t in pricing.versions()
            if (t["effective_date"], t["version"]) > (active["effective_date"], active["version"])
        ]
        return {
            "active": {"version": active["version"], "effective_date": active["effective_date"],
                       "notes": active.get("notes", ""), "confirmed_at": state.get("confirmed_at"),
                       "bootstrap": bool(state.get("bootstrap"))},
            "pending": newer,
            "unverified": pricing.unverified_sections(active),
        }

    def confirm_price(self, version: str) -> dict:
        state = self.price_state()
        if version not in {p["version"] for p in state["pending"]}:
            raise ValueError(f"版本 {version} 不是待确认的新版本")
        self.db.set("price_active", {"version": version, "confirmed_at": iso(self.clock()), "bootstrap": False})
        for s in self.stores():
            self.rebuild_latest_snapshot(s["sid"])
        return self.price_state()

    # ------------------------------------------------------------------ 店铺
    def refresh_stores(self) -> list[dict]:
        rows = self.api.sellers()
        now = iso(self.clock())
        kept = []
        with self.db.tx() as c:
            for r in rows:
                site = site_for_seller(r)
                if site is None or r.get("sid") in (None, ""):
                    continue
                sid = int(r["sid"])
                c.execute(
                    "INSERT INTO stores(sid,name,site,raw,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(sid) DO UPDATE "
                    "SET name=excluded.name, site=excluded.site, raw=excluded.raw, updated_at=excluded.updated_at",
                    (sid, str(r.get("name") or sid), site.code, json.dumps(r, ensure_ascii=False), now),
                )
                kept.append(sid)
            if kept:
                c.execute(f"DELETE FROM stores WHERE sid NOT IN ({','.join('?' * len(kept))})", kept)
        self.db.set("stores_refreshed_at", now)
        return self.stores()

    # ------------------------------------------------------------------ 月任务
    def run_monthly(self, sid: int) -> dict[str, str]:
        st = self.store(sid)
        assert st
        today = self.local_today(st["site"])
        results = {}
        plan = (
            (TASK_LISTING, month_str(today), lambda: self.task_listing(sid)),
            (TASK_STORAGE_MONTH, month_str(prev_month(today)), lambda: self.task_storage_month(sid, prev_month(today))),
            (TASK_RETURN_RATE, month_str(today), lambda: self.task_return_rate(sid, today)),
        )
        for task, period, fn in plan:
            s = self._status(sid, task, period)
            if s and s["status"] == "success":
                results[task] = "success"
                continue
            try:
                status, msg = fn()
            except Exception as exc:  # noqa: BLE001
                log.exception("月任务 %s sid=%s 失败", task, sid)
                status, msg = "failed", str(exc)
            self._set_status(sid, task, period, status, msg)
            results[task] = status
        return results

    def task_listing(self, sid: int) -> tuple[str, str]:
        rows = self.api.listing(sid)
        now = iso(self.clock())
        mapping: dict[str, str] = {}
        conflicts = 0
        for r in rows:
            fnsku = str(r.get("fnsku") or "").strip()
            msku = str(r.get("seller_sku") or r.get("msku") or "").strip()
            if not fnsku or not msku:
                continue
            if fnsku in mapping and mapping[fnsku] != msku:
                conflicts += 1
                continue
            mapping[fnsku] = msku
        with self.db.tx() as c:
            c.execute("DELETE FROM listing_map WHERE sid=?", (sid,))
            c.executemany("INSERT INTO listing_map(sid,fnsku,msku,updated_at) VALUES(?,?,?,?)",
                          [(sid, f, m, now) for f, m in mapping.items()])
        return "success", f"{len(mapping)} 条 fnsku 映射" + (f"，{conflicts} 条一对多保留首条" if conflicts else "")

    def task_storage_month(self, sid: int, month_day: date) -> tuple[str, str]:
        month = month_str(month_day)
        rows = self.api.storage_fee_month(sid, month)
        if not rows:
            return "not_ready", f"{month} 月仓储费报告未出"
        now = iso(self.clock())
        with self.db.tx() as c:
            c.execute("DELETE FROM storage_fee_rows WHERE sid=? AND month=?", (sid, month))
            for r in rows:
                fnsku = str(r.get("fnsku") or "").strip()
                if not fnsku:
                    continue
                fc = str(r.get("fulfillment_center") or r.get("fulfillment_center_id") or "")
                c.execute(
                    "INSERT INTO storage_fee_rows(sid,fnsku,month,fulfillment_center,row,fetched_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(sid,fnsku,month,fulfillment_center) DO UPDATE SET row=excluded.row, "
                    "fetched_at=excluded.fetched_at",
                    (sid, fnsku, month, fc, json.dumps(r, ensure_ascii=False), now),
                )
        return "success", f"{month} 共 {len(rows)} 行"

    def task_return_rate(self, sid: int, today: date) -> tuple[str, str]:
        start = today - timedelta(days=Q_WINDOW_START_DAYS)
        end = today - timedelta(days=Q_LAG_DAYS)
        rows = self.api.order_profit([sid], f"{start} 00:00:00", f"{end} 23:59:59")
        parsed: dict[str, tuple[float, bool, str]] = {}
        for raw in rows:
            n = normalize_row(raw, default_sid=sid)
            if not n or n["sid"] != sid:
                continue
            q, flagged = parse_return_rate(n["return_rate_raw"])
            if q is None or n["msku"] in parsed:
                continue
            parsed[n["msku"]] = (q, flagged, str(n["return_rate_raw"]))
        now = iso(self.clock())
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO return_rate_versions(sid,window_start,window_end,computed_at) VALUES(?,?,?,?)",
                (sid, start.isoformat(), end.isoformat(), now),
            )
            vid = cur.lastrowid
            c.executemany(
                "INSERT INTO return_rates(version_id,sid,msku,q,raw,flagged) VALUES(?,?,?,?,?,?)",
                [(vid, sid, m, q, raw, int(fl)) for m, (q, fl, raw) in parsed.items()],
            )
        flagged = sum(1 for v in parsed.values() if v[1])
        return "success", f"{len(parsed)} 个 msku 有 Q" + (f"，{flagged} 个数值>1 已按百分数处理" if flagged else "")

    # ------------------------------------------------------------------ 日任务
    def daily_due(self, sid: int, now: datetime | None = None, ignore_time: bool = False) -> bool:
        st = self.store(sid)
        if not st:
            return False
        now = now or self.clock()
        ln = local_now(st["site"].tz, now)
        s = self._status(sid, TASK_DAILY, ln.date().isoformat())
        if s and s["status"] == "success":
            return False
        if s and (now - datetime.fromisoformat(s["updated_at"])) < timedelta(minutes=30):
            return False
        if ignore_time:
            return True
        hh, mm = (int(x) for x in self.cfg.daily_local_time.split(":"))
        return (ln.hour, ln.minute) >= (hh, mm)

    def run_daily(self, sid: int) -> str:
        st = self.store(sid)
        assert st
        site = st["site"]
        now = self.clock()
        today = self.local_today(site, now)
        try:
            self.run_monthly(sid)
            self.task_fx(site, today)
            self.task_age_snapshot(sid)
            self.task_receipts(sid, site, today, now)
            if self.db.get(fifo.t0_key(sid)) is None:
                self.open_books(sid, self.clock())
        except Exception as exc:  # noqa: BLE001
            log.exception("日任务 sid=%s 失败", sid)
            self._set_status(sid, TASK_DAILY, today.isoformat(), "failed", str(exc))
            return "failed"
        self._set_status(sid, TASK_DAILY, today.isoformat(), "success", "")
        return "success"

    def task_fx(self, site: Site, today: date) -> None:
        current = fx_rate(self.db, site.currency, today)
        if current and not current.is_fallback:
            return
        for month in fx_months_to_try(today):
            rows = self.api.currency_month(month)
            now = iso(self.clock())
            found = False
            with self.db.tx() as c:
                for r in rows:
                    code = str(r.get("code") or "").upper()
                    if not code:
                        continue
                    my_rate, rate_org = num(r.get("my_rate")), num(r.get("rate_org"))
                    m = str(r.get("date") or month)[:7]
                    c.execute(
                        "INSERT INTO fx_rates(month,code,my_rate,rate_org,fetched_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(month,code) DO UPDATE SET my_rate=excluded.my_rate, rate_org=excluded.rate_org, "
                        "fetched_at=excluded.fetched_at",
                        (m, code, my_rate or None, rate_org or None, now),
                    )
                    if code == site.currency and (my_rate > 0 or rate_org > 0):
                        found = True
            if found:
                return

    def task_age_snapshot(self, sid: int) -> list[dict]:
        rows = self.api.fba_age_list(sid)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO age_snapshots(sid,fetched_at,snapshot_date,rows) VALUES(?,?,?,?)",
                (sid, iso(self.clock()), snapshot_date_of(rows), json.dumps(rows, ensure_ascii=False)),
            )
            # 只保留最近 3 份，开账用的那份另存在批次里
            c.execute(
                "DELETE FROM age_snapshots WHERE sid=? AND id NOT IN "
                "(SELECT id FROM age_snapshots WHERE sid=? ORDER BY id DESC LIMIT 3)",
                (sid, sid),
            )
        return rows

    def open_books(self, sid: int, now: datetime) -> int:
        snap = load_age_snapshot(self.db, sid)
        if snap is None:
            return 0
        opening = {m: int(round(sum(b.values()))) for m, b in snap.by_msku.items()}
        n = fifo.open_books(self.db, sid, now, opening)
        self.db.set(f"fifo_t0_snapshot:{sid}", {"age_snapshot_id": snap.id, "snapshot_date": snap.snapshot_date,
                                               "lots": n, "units": sum(opening.values())})
        return n

    def task_receipts(self, sid: int, site: Site, today: date, now: datetime) -> int:
        t0s = self.db.get(fifo.t0_key(sid))
        if not t0s:
            return 0
        t0 = datetime.fromisoformat(t0s)
        t0_local = t0.astimezone(site.tz)
        last = self.db.get(f"receipts_through:{sid}")
        start = max(t0_local.date(), (date.fromisoformat(last) - timedelta(days=1)) if last else t0_local.date())
        rows = self.api.received_inventory(sid, start.isoformat(), today.isoformat())
        mapping = {r["fnsku"]: r["msku"] for r in
                   self.db.query("SELECT fnsku, msku FROM listing_map WHERE sid=?", (sid,))}
        applied = 0
        with self.db.tx() as c:
            for r in fifo.new_receipts(c, sid, rows):
                raw_date = r.get("received_date_report") or r.get("received_date")
                rd_dt = parse_local_datetime(raw_date, site.tz)
                rd = parse_date(raw_date)
                if rd is None:
                    self.db.anomaly(iso(now), sid, None, "receipt_no_date", json.dumps(r, ensure_ascii=False)[:500],
                                    conn=c)
                    continue
                after_t0 = rd_dt > t0 if rd_dt is not None else rd > t0_local.date()
                if not after_t0:
                    continue
                msku = str(r.get("sku") or r.get("msku") or "") or mapping.get(str(r.get("fnsku") or ""), "")
                if not msku:
                    self.db.anomaly(iso(now), sid, None, "receipt_unmapped",
                                    f"签收行 fnsku={r.get('fnsku')} 无法对应 msku", conn=c)
                    continue
                qty = int(round(num(r.get("quantity"))))
                fifo.apply_receipt(self.db, c, sid, msku, rd, qty, now)
                applied += 1
            self.db.set(f"receipts_through:{sid}", today.isoformat(), conn=c)
        return applied

    # ------------------------------------------------------------------ 15 分钟轮询
    def poll(self, run_id: int | None = None) -> dict:
        groups: dict[str, list[int]] = {}
        for s in self.stores():
            groups.setdefault(s["site"].code, []).append(s["sid"])
        errors = {}
        for code, sids in groups.items():
            site = SITES[code]
            try:
                self._poll_group(site, sids, run_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("轮询 %s 失败", code)
                errors[code] = str(exc)
        self._maybe_sign_check()
        return errors

    def _fmt(self, d: date, dt: datetime | None) -> str:
        if not self.cfg.order_profit_datetime:
            return d.isoformat()
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else f"{d} 00:00:00"

    def _poll_group(self, site: Site, sids: list[int], run_id: int | None) -> None:
        now = self.clock()
        ln = local_now(site.tz, now)
        today = ln.date()
        if self.cfg.fifo_close_previous_day:
            self._close_previous_day(site, sids, today)
        raw = self.api.order_profit(sids, self._fmt(today, None), self._fmt(today, ln.replace(microsecond=0)))
        default_sid = sids[0] if len(sids) == 1 else None
        normalized = [n for n in (normalize_row(r, default_sid) for r in raw) if n and n["sid"] in sids]
        agg = aggregate(normalized)
        fetched = iso(now)
        with self.db.tx() as c:
            placeholders = ",".join("?" * len(sids))
            c.execute(f"DELETE FROM profit_latest WHERE local_date=? AND sid IN ({placeholders})",
                      (today.isoformat(), *sids))
            for (sid, msku), row in agg.items():
                c.execute("INSERT INTO profit_latest(sid,local_date,msku,row,fetched_at) VALUES(?,?,?,?,?)",
                          (sid, today.isoformat(), msku, json.dumps(row, ensure_ascii=False), fetched))
            for sid in sids:
                if c.execute("SELECT 1 FROM kv WHERE key=?", (fifo.t0_key(sid),)).fetchone() is None:
                    continue
                for (rsid, msku), row in agg.items():
                    if rsid == sid:
                        fifo.consume(self.db, c, sid, msku, today, int(round(max(0.0, row["afn_volume"]))), now)
        for sid in sids:
            self.build_snapshot(sid, now, run_id)

    def _close_previous_day(self, site: Site, sids: list[int], today: date) -> None:
        yday = today - timedelta(days=1)
        need = []
        for sid in sids:
            if self.db.get(fifo.t0_key(sid)) is None or self.db.get(f"fifo_closed:{sid}:{yday}"):
                continue
            if self.db.one("SELECT 1 FROM fifo_daily WHERE sid=? AND local_date=?", (sid, yday.isoformat())):
                need.append(sid)
        if not need:
            return
        end_local = datetime.combine(yday, datetime.max.time().replace(microsecond=0), tzinfo=site.tz)
        raw = self.api.order_profit(need, self._fmt(yday, None), self._fmt(yday, end_local))
        default_sid = need[0] if len(need) == 1 else None
        agg = aggregate([n for n in (normalize_row(r, default_sid) for r in raw) if n and n["sid"] in need])
        with self.db.tx() as c:
            for (sid, msku), row in agg.items():
                fifo.consume(self.db, c, sid, msku, yday, int(round(max(0.0, row["afn_volume"]))), end_local)
            for sid in need:
                self.db.set(f"fifo_closed:{sid}:{yday}", True, conn=c)

    # ------------------------------------------------------------------ 快照
    def build_context(self, sid: int, now: datetime) -> StoreContext:
        st = self.store(sid)
        assert st
        site: Site = st["site"]
        today = self.local_today(site, now)
        profit = {r["msku"]: json.loads(r["row"]) for r in
                  self.db.query("SELECT msku, row FROM profit_latest WHERE sid=? AND local_date=?",
                                (sid, today.isoformat()))}
        pending = self._status(sid, TASK_STORAGE_MONTH, month_str(prev_month(today)))
        return StoreContext(
            sid=sid, name=st["name"], site=site, now=now, local_date=today, profit=profit,
            q=load_return_rates(self.db, sid), dims=load_dims(self.db, sid), age=load_age_snapshot(self.db, sid),
            fx=fx_rate(self.db, site.currency, today), price=self.active_price(),
            sold=fifo.sold_today(self.db, sid, today), t0=self.db.get(fifo.t0_key(sid)),
            cost_sign=self.cfg.cost_sign,
            storage_report_pending=not (pending and pending["status"] == "success"),
        )

    def _summary_meta(self, ctx: StoreContext, rows: list[dict]) -> dict:
        yday = ctx.local_date - timedelta(days=1)
        age_date = parse_date(ctx.age.snapshot_date) if ctx.age else None
        ln = local_now(ctx.site.tz, ctx.now)
        unmapped = set()
        if ctx.dims.latest_month:
            unmapped |= ctx.dims.unmapped.get(ctx.dims.latest_month, set())
        if ctx.age:
            unmapped |= ctx.age.unmapped_fnskus
        return {
            "sid": ctx.sid,
            "name": ctx.name,
            "site": ctx.site.code,
            "site_name": ctx.site.name,
            "tz": ctx.site.tz_name,
            "currency": ctx.site.currency,
            "local_date": ctx.local_date.isoformat(),
            "local_time": ln.strftime("%H:%M"),
            "elapsed_fraction": ctx.elapsed,
            "fx": ({"code": ctx.fx.code, "month": ctx.fx.month, "rate": ctx.fx.rate, "source": ctx.fx.source,
                    "fallback": ctx.fx.is_fallback} if ctx.fx else None),
            "q_window": ({"start": ctx.q.window_start, "end": ctx.q.window_end, "computed_at": ctx.q.computed_at}
                         if ctx.q.version_id else None),
            "age_snapshot_date": ctx.age.snapshot_date if ctx.age else None,
            "age_fetched_at": ctx.age.fetched_at if ctx.age else None,
            "age_stale": bool(ctx.age and (age_date is None or age_date < yday)),
            "dims_month": ctx.dims.latest_month,
            "dims_history": bool(ctx.dims.months),
            "storage_report_pending": ctx.storage_report_pending,
            "unmapped_fnskus": sorted(unmapped)[:50],
            "unmapped_fnsku_count": len(unmapped),
            "price_version": ctx.price["version"],
            "t0": ctx.t0,
            "has_profit_data": bool(ctx.profit),
        }

    def compute(self, sid: int, now: datetime) -> tuple[dict, list[dict]]:
        ctx = self.build_context(sid, now)
        rows = compute_rows(ctx)
        summary = {**summarize(rows), **self._summary_meta(ctx, rows)}
        return summary, rows

    def build_snapshot(self, sid: int, now: datetime, run_id: int | None) -> int:
        summary, rows = self.compute(sid, now)
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO snapshots(sid,local_date,taken_at,poll_run_id,summary,rows) VALUES(?,?,?,?,?,?)",
                (sid, summary["local_date"], iso(now), run_id, json.dumps(summary, ensure_ascii=False),
                 json.dumps(rows, ensure_ascii=False)),
            )
            c.execute("UPDATE snapshots SET rows=NULL WHERE sid=? AND local_date<? AND rows IS NOT NULL",
                      (sid, summary["local_date"]))
            return int(cur.lastrowid)

    def rebuild_latest_snapshot(self, sid: int) -> None:
        """价目版本切换后按同一时刻重算最新一张快照，不新增曲线点、不请求接口。"""
        r = self.db.one("SELECT id, taken_at FROM snapshots WHERE sid=? ORDER BY id DESC LIMIT 1", (sid,))
        if not r:
            return
        summary, rows = self.compute(sid, datetime.fromisoformat(r["taken_at"]))
        with self.db.tx() as c:
            c.execute("UPDATE snapshots SET summary=?, rows=? WHERE id=?",
                      (json.dumps(summary, ensure_ascii=False), json.dumps(rows, ensure_ascii=False), r["id"]))

    # ------------------------------------------------------------------ 符号验收
    def _maybe_sign_check(self) -> None:
        """每店取一个样本 msku 手算；任何一店不通过即判定失败。通过后不再重复，改了 COST_SIGN 会重验。"""
        state = self.db.get("sign_check")
        if state and state.get("cost_sign") == self.cfg.cost_sign and state["status"] in ("passed", "failed"):
            return
        results = []
        for s in self.stores():
            r = self.db.one("SELECT rows FROM snapshots WHERE sid=? AND rows IS NOT NULL ORDER BY id DESC LIMIT 1",
                            (s["sid"],))
            if r:
                res = sign_check(s["sid"], json.loads(r["rows"]), self.cfg.cost_sign)
                if res:
                    results.append(res)
        if not results:
            self.db.set("sign_check", {"status": "pending", "cost_sign": self.cfg.cost_sign,
                                       "checked_at": iso(self.clock())})
            return
        failed = [x for x in results if x["status"] == "failed"]
        head = failed[0] if failed else results[0]
        self.db.set("sign_check", {**head, "status": "failed" if failed else "passed",
                                   "samples": [{"sid": x["sid"], "msku": x["msku"], "status": x["status"]}
                                               for x in results],
                                   "checked_at": iso(self.clock())})

    # ------------------------------------------------------------------ 首次启动
    def bootstrap(self) -> None:
        """第一次启动立刻做完月任务、日任务和开账，再进入 15 分钟循环。"""
        self.refresh_stores()
        for s in self.stores():
            if self.daily_due(s["sid"], ignore_time=True):
                self.run_daily(s["sid"])
