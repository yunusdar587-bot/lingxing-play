from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .jobs import Engine
from .timeutil import iso, utcnow

log = logging.getLogger(__name__)


def next_boundary(now: datetime, minutes: int) -> datetime:
    base = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    step = minutes - (base.minute % minutes)
    return base + timedelta(minutes=step)


class Scheduler:
    """15 分钟轮询与日/月任务分开执行：轮询一个线程，日/月任务一个串行工作线程。"""

    def __init__(self, engine: Engine, poll_minutes: int = 15, clock: Callable[[], datetime] = utcnow,
                 tick_seconds: float = 20.0):
        self.engine = engine
        self.db = engine.db
        self.poll_minutes = poll_minutes
        self.clock = clock
        self.tick_seconds = tick_seconds
        self._poll_lock = threading.Lock()
        self._poll_started: datetime | None = None
        self._jobs: "queue.Queue[int]" = queue.Queue()
        self._queued: set[int] = set()
        self._queued_lock = threading.Lock()
        self._stop = threading.Event()
        self.ready = threading.Event()

    # ---- 轮询 ----
    def trigger_poll(self, scheduled_at: datetime) -> int | None:
        if not self._poll_lock.acquire(blocking=False):
            started = self._poll_started
            delay = (self.clock() - started).total_seconds() if started else 0
            msg = f"上一轮仍在运行（已运行 {delay:.0f} 秒），本轮跳过"
            log.warning(msg)
            with self.db.tx() as c:
                c.execute("INSERT INTO poll_runs(scheduled_at,status,message) VALUES(?,?,?)",
                          (iso(scheduled_at), "skipped", msg))
            return None
        self._poll_started = self.clock()
        with self.db.tx() as c:
            run_id = c.execute("INSERT INTO poll_runs(scheduled_at,started_at,status) VALUES(?,?,?)",
                               (iso(scheduled_at), iso(self._poll_started), "running")).lastrowid
        threading.Thread(target=self._run_poll, args=(run_id,), name="poll", daemon=True).start()
        return run_id

    def _run_poll(self, run_id: int) -> None:
        try:
            errors = self.engine.poll(run_id)
            status, msg = ("partial", "; ".join(f"{k}: {v}" for k, v in errors.items())) if errors else ("ok", "")
        except Exception as exc:  # noqa: BLE001
            log.exception("轮询失败")
            status, msg = "failed", str(exc)
        finally:
            self._poll_lock.release()
        with self.db.tx() as c:
            c.execute("UPDATE poll_runs SET finished_at=?, status=?, message=? WHERE id=?",
                      (iso(self.clock()), status, msg[:2000], run_id))

    # ---- 日/月任务 ----
    def _enqueue(self, sid: int) -> None:
        with self._queued_lock:
            if sid in self._queued:
                return
            self._queued.add(sid)
        self._jobs.put(sid)

    def _job_worker(self) -> None:
        while not self._stop.is_set():
            try:
                sid = self._jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.engine.run_daily(sid)
            finally:
                with self._queued_lock:
                    self._queued.discard(sid)

    # ---- 主循环 ----
    def run_forever(self) -> None:
        try:
            self.engine.bootstrap()
        except Exception:  # noqa: BLE001
            log.exception("首次启动任务失败，进入循环后按日任务重试")
        threading.Thread(target=self._job_worker, name="jobs", daemon=True).start()
        self.trigger_poll(self.clock())
        self.ready.set()
        next_poll = next_boundary(self.clock(), self.poll_minutes)
        last_store_refresh = self.clock()
        while not self._stop.is_set():
            now = self.clock()
            if now >= next_poll:
                self.trigger_poll(next_poll)
                next_poll = next_boundary(now, self.poll_minutes)
            if now - last_store_refresh > timedelta(hours=24):
                try:
                    self.engine.refresh_stores()
                except Exception:  # noqa: BLE001
                    log.exception("刷新店铺列表失败")
                last_store_refresh = now
            for s in self.engine.stores():
                if self.engine.daily_due(s["sid"], now):
                    self._enqueue(s["sid"])
            self._stop.wait(self.tick_seconds)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="scheduler", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


def wait_until(predicate: Callable[[], bool], timeout: float = 30.0, step: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
