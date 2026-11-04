from __future__ import annotations

from datetime import datetime, timezone

from lingxing_dashboard.config import Config
from lingxing_dashboard.db import Database
from lingxing_dashboard.demo import DEMO_APP_ID, DEMO_SECRET, FakeLingxing
from lingxing_dashboard.jobs import Engine
from lingxing_dashboard.lingxing import LingxingApi, LingxingClient


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def set(self, *args) -> None:
        self.dt = datetime(*args, tzinfo=timezone.utc)


def make_engine(start: datetime | None = None, **cfg_kwargs):
    clock = Clock(start or datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc))
    fake = FakeLingxing(clock=clock)
    cfg = Config(db_path=":memory:", **cfg_kwargs)
    db = Database(":memory:")
    client = LingxingClient(DEMO_APP_ID, DEMO_SECRET, "http://fake", transport=fake, sleep=lambda s: None)
    engine = Engine(cfg, db, LingxingApi(client, cfg.page_length), clock=clock)
    return engine, fake, clock, db
