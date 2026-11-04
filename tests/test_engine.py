import json
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from lingxing_dashboard import fifo
from lingxing_dashboard.calc import MODE_DAILY, MODE_ORDER
from lingxing_dashboard.scheduler import Scheduler
from lingxing_dashboard.web import DashboardService

from tests.helpers import make_engine

US = 101


def profit_for(fake, units_by_msku, net_per_unit=100.0):
    """固定 US 店铺的 OrderProfit 返回，便于检验覆盖与增量。"""
    def override(sids, start, end):
        if sids != [US] and US not in sids:
            return None
        if (datetime.fromisoformat(end[:10]) - datetime.fromisoformat(start[:10])).days > 31:
            return None
        rows = []
        for msku, units in units_by_msku().items():
            rows.append({"sid": US, "msku": msku, "afn_volume": units, "net_amount": units * net_per_unit,
                         "selling_fee": -units * 15, "gross_profit": units * 30, "refund_amount": -units * 2,
                         "fba_storage_fee": -1, "long_term_storage_fee": 0})
        return rows
    fake.profit_override = override


class EngineFlowTest(unittest.TestCase):
    def setUp(self):
        # 洛杉矶 2026-11-04 10:00
        self.engine, self.fake, self.clock, self.db = make_engine(datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc))
        self.units = {"US-LAMP-01": 3}
        profit_for(self.fake, lambda: dict(self.units))
        self.fake.age_override[US] = [{"sid": US, "sku": "US-LAMP-01", "fnsku": "F1", "snapshot_date": "2026-11-03",
                                       "inv_age_0_to_30_days": 3, "inv_age_31_to_60_days": 2,
                                       "inv_age_0_to_90_days": 5}]

    def test_bootstrap_filters_sites_and_opens_books(self):
        self.engine.bootstrap()
        self.assertEqual(sorted(s["sid"] for s in self.engine.stores()), [101, 102, 103, 104])
        self.assertIsNotNone(self.db.get(fifo.t0_key(US)))
        lots = self.db.query("SELECT * FROM fifo_lots WHERE sid=? AND msku='US-LAMP-01'", (US,))
        self.assertEqual([(r["kind"], r["qty_in"]) for r in lots], [("opening", 5)])
        # Q 请求窗口：今天往前 395 天到往前 30 天
        q_calls = [p for path, p in self.fake.calls if path.endswith("OrderProfit") and p["sids"] == [US]]
        self.assertEqual(q_calls[0]["startDate"], "2025-10-05 00:00:00")
        self.assertEqual(q_calls[0]["endDate"], "2026-10-05 23:59:59")

    def test_poll_overwrites_not_accumulates_and_digests_increment(self):
        self.engine.bootstrap()
        self.engine.poll()
        self.units["US-LAMP-01"] = 4
        self.clock.dt += timedelta(minutes=15)
        self.engine.poll()
        row = json.loads(self.db.one("SELECT row FROM profit_latest WHERE sid=? AND msku='US-LAMP-01'", (US,))["row"])
        self.assertEqual(row["afn_volume"], 4)
        daily = self.db.one("SELECT consumed FROM fifo_daily WHERE sid=? AND msku='US-LAMP-01'", (US,))
        self.assertEqual(daily["consumed"], 4)
        remaining = self.db.one("SELECT qty_remaining FROM fifo_lots WHERE sid=? AND kind='opening'", (US,))
        self.assertEqual(remaining["qty_remaining"], 1)
        poll_call = [p for path, p in self.fake.calls if path.endswith("OrderProfit") and p["sids"] == [US]][-1]
        self.assertEqual(poll_call["startDate"], "2026-11-04 00:00:00")
        self.assertEqual(poll_call["endDate"], "2026-11-04 10:15:00")
        self.assertEqual(poll_call["currencyCode"], "CNY")
        self.assertEqual(poll_call["length"], 5000)

    def test_negative_increment_records_anomaly(self):
        self.engine.bootstrap()
        self.engine.poll()
        self.units["US-LAMP-01"] = 1
        self.engine.poll()
        a = self.db.query("SELECT kind FROM anomalies WHERE sid=? AND kind='negative_increment'", (US,))
        self.assertEqual(len(a), 1)
        remaining = self.db.one("SELECT qty_remaining FROM fifo_lots WHERE sid=? AND kind='opening'", (US,))
        self.assertEqual(remaining["qty_remaining"], 2)

    def test_multi_day_order_storage_becomes_ready(self):
        self.engine.cfg.fifo_close_previous_day = False
        self.engine.bootstrap()
        self.engine.poll()                                       # 11-04 卖 3 件，开账剩 2 件
        # 11-05：签收 10 件；当天卖 2 件仍来自开账批次
        self.fake.add_receipt(US, "LAMP-01", date(2026, 11, 5), 10)
        self.fake.add_receipt(US, "LAMP-01", date(2026, 11, 5), -1)
        self.clock.dt = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)   # 洛杉矶 07:00
        self.engine.run_daily(US)
        self.units["US-LAMP-01"] = 2
        self.engine.poll()
        lots = self.db.query("SELECT kind, receive_date, qty_in, qty_remaining FROM fifo_lots WHERE sid=? "
                             "ORDER BY id", (US,))
        self.assertEqual([(r["kind"], r["qty_remaining"]) for r in lots], [("opening", 0), ("received", 9)])
        summary, rows = self.engine.compute(US, self.clock())
        lamp = next(r for r in rows if r["msku"] == "US-LAMP-01")
        self.assertEqual(lamp["order_storage_status"], "unknown_lot")
        # 11-06：开账批次已消化完，新卖出的全部来自已知签收
        self.clock.dt = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)
        self.units["US-LAMP-01"] = 3
        self.engine.poll()
        summary, rows = self.engine.compute(US, self.clock())
        lamp = next(r for r in rows if r["msku"] == "US-LAMP-01")
        self.assertEqual(lamp["order_storage_status"], "ok")
        self.assertEqual(lamp["order_known_units"], 3)
        self.assertGreater(lamp["order_storage_cny"], 0)
        self.assertFalse(summary[MODE_ORDER]["fallback_to_daily"])

    def test_previous_day_close_digests_tail(self):
        self.engine.bootstrap()
        self.engine.poll()  # 11-04 10:00 看到 3 件
        self.units["US-LAMP-01"] = 5  # 11-04 全天合计 5 件，次日首轮前补一次上一自然日
        self.clock.dt = datetime(2026, 11, 5, 8, 5, tzinfo=timezone.utc)  # 洛杉矶 11-05 00:05
        self.engine.poll()
        close = [p for path, p in self.fake.calls if path.endswith("OrderProfit") and p["sids"] == [US]][-2]
        self.assertEqual((close["startDate"], close["endDate"]), ("2026-11-04 00:00:00", "2026-11-04 23:59:59"))
        rows = {r["local_date"]: r["consumed"] for r in
                self.db.query("SELECT local_date, consumed FROM fifo_daily WHERE sid=?", (US,))}
        self.assertEqual(rows["2026-11-04"], 5)

    def test_sign_check_failure_hides_adjusted(self):
        def bad(sids, start, end):
            if US not in sids or (datetime.fromisoformat(end[:10]) - datetime.fromisoformat(start[:10])).days > 31:
                return None
            return [{"sid": US, "msku": "US-LAMP-01", "afn_volume": 2, "net_amount": 200, "selling_fee": -30,
                     "gross_profit": 50, "refund_amount": 15, "fba_storage_fee": 4}]
        self.fake.profit_override = bad
        self.engine.bootstrap()
        self.engine.poll()
        state = self.db.get("sign_check")
        self.assertEqual(state["status"], "failed")
        d = DashboardService(self.engine).dashboard()
        self.assertTrue(d["adjusted_hidden"])
        self.assertIsNone(d["total"]["amounts"])
        detail = DashboardService(self.engine).store_detail(US)
        self.assertTrue(all(r["adjusted"][MODE_DAILY] is None for r in detail["rows"]))


class PriceConfirmTest(unittest.TestCase):
    def test_new_version_needs_confirmation(self):
        engine, fake, clock, db = make_engine()
        engine.bootstrap()
        engine.poll()
        st = engine.price_state()
        self.assertEqual(st["active"]["version"], "2025.1")
        self.assertEqual([p["version"] for p in st["pending"]], ["2026.1"])
        n_snap = db.one("SELECT COUNT(*) n FROM snapshots")["n"]
        with self.assertRaises(ValueError):
            engine.confirm_price("2025.1")
        engine.confirm_price("2026.1")
        self.assertEqual(engine.price_state()["active"]["version"], "2026.1")
        self.assertEqual(engine.price_state()["pending"], [])
        self.assertEqual(db.one("SELECT COUNT(*) n FROM snapshots")["n"], n_snap)
        latest = json.loads(db.one("SELECT summary FROM snapshots ORDER BY id DESC LIMIT 1")["summary"])
        self.assertEqual(latest["price_version"], "2026.1")


class SchedulerTest(unittest.TestCase):
    def test_skip_when_previous_poll_running(self):
        engine, fake, clock, db = make_engine()
        engine.bootstrap()
        gate = threading.Event()
        original = engine.poll

        def slow_poll(run_id=None):
            gate.wait(5)
            return original(run_id)

        engine.poll = slow_poll
        sch = Scheduler(engine)
        first = sch.trigger_poll(clock())
        second = sch.trigger_poll(clock() + timedelta(minutes=15))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        gate.set()
        skipped = db.query("SELECT * FROM poll_runs WHERE status='skipped'")
        self.assertEqual(len(skipped), 1)
        self.assertIn("跳过", skipped[0]["message"])


if __name__ == "__main__":
    unittest.main()
