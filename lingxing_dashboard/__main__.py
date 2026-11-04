from __future__ import annotations

import argparse
import logging
import sys

from .config import Config
from .db import Database
from .jobs import Engine
from .lingxing import LingxingApi, LingxingClient
from .scheduler import Scheduler
from .web import DashboardService, serve


def build(cfg: Config) -> tuple[Engine, Database]:
    db = Database(cfg.db_path)
    if cfg.demo:
        from .demo import DEMO_APP_ID, DEMO_SECRET, FakeLingxing

        client = LingxingClient(DEMO_APP_ID, DEMO_SECRET, "http://demo.local", transport=FakeLingxing())
    else:
        if not cfg.app_id or not cfg.app_secret:
            sys.exit("缺少 LINGXING_APP_ID / LINGXING_APP_SECRET（可写在 .env），或用 --demo 运行演示数据")
        client = LingxingClient(cfg.app_id, cfg.app_secret, cfg.base_url)
    return Engine(cfg, db, LingxingApi(client, cfg.page_length)), db


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m lingxing_dashboard", description="领星四站当日调整后利润看板")
    ap.add_argument("--demo", action="store_true", help="使用内置假数据，不连接领星")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--db")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    cfg = Config.from_env(args.env_file)
    if args.demo:
        cfg.demo = True
        if not args.db:
            cfg.db_path = "data/demo.sqlite3"
    if args.port:
        cfg.port = args.port
    if args.host:
        cfg.host = args.host
    if args.db:
        cfg.db_path = args.db
    engine, _db = build(cfg)
    Scheduler(engine, cfg.poll_minutes).start()
    httpd = serve(DashboardService(engine), cfg.host, cfg.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
