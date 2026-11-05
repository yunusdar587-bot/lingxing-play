import unittest
from datetime import datetime, timedelta, timezone

from lingxing_dashboard.history import floor_local_slot, parse_slot
from lingxing_dashboard.sites import SITES
from lingxing_dashboard.timeutil import local_now
from lingxing_dashboard.web import DashboardService

from tests.helpers import make_engine
from tests.test_engine import US, profit_for


class HistoryStoreTest(unittest.TestCase):
    def test_slot_must_fall_on_quarter_hour(self):
        self.assertEqual(parse_slot("2026-11-04 10:15").minute, 15)
        self.assertEqual(parse_slot("2026-11-04T10:00").minute, 0)
        with self.assertRaises(ValueError):
            parse_slot("2026-11-04 10:07")

    def test_floor_uses_the_sites_own_clock(self):
        now = datetime(2026, 11, 4, 18, 7, tzinfo=timezone.utc)
        self.assertEqual(floor_local_slot(local_now(SITES["US"].tz, now)), "2026-11-04 10:00")
        self.assertEqual(floor_local_slot(local_now(SITES["CA"].tz, now)), "2026-11-04 13:00")

    def test_range_keeps_each_store_on_its_own_clock(self):
        engine, fake, _clock, _db = make_engine(datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc))
        profit_for(fake, lambda: {"US-LAMP-01": 3})
        engine.bootstrap()
        engine.poll()
        view = DashboardService(engine).history_query("2026-11-04 10:00", "2026-11-04 10:00")
        by_sid = {s["sid"]: s for s in view["stores"]}
        self.assertEqual(view["slot_count"], 1)
        self.assertEqual(by_sid[US]["kept"], 1)
        self.assertTrue(by_sid[US]["points"][0]["has_payload"])
        self.assertEqual(by_sid[US]["points"][0]["afn_volume"], 3)
        self.assertEqual(by_sid[102]["kept"], 0)
        detail = engine.history.detail(US, "2026-11-04 10:00")
        self.assertEqual(detail["rows"][0]["msku"], "US-LAMP-01")
        self.assertEqual(detail["rows"][0]["afn_volume"], 3)
        ca = DashboardService(engine).history_query("2026-11-04 13:00", "2026-11-04 13:00")
        ca_by = {s["sid"]: s for s in ca["stores"]}
        self.assertEqual(ca_by[102]["kept"], 1)
        self.assertEqual(ca_by[US]["kept"], 0)

    def test_later_poll_does_not_erase_the_earlier_slot(self):
        engine, fake, clock, _db = make_engine(datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc))
        units = {"US-LAMP-01": 3}
        profit_for(fake, lambda: dict(units))
        engine.bootstrap()
        engine.poll()
        units["US-LAMP-01"] = 4
        clock.dt += timedelta(minutes=15)
        engine.poll()
        early = engine.history.detail(US, "2026-11-04 10:00")
        late = engine.history.detail(US, "2026-11-04 10:15")
        self.assertEqual(early["rows"][0]["afn_volume"], 3)
        self.assertEqual(late["rows"][0]["afn_volume"], 4)

    def test_summary_import_does_not_wipe_payload(self):
        engine, fake, _clock, db = make_engine(datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc))
        profit_for(fake, lambda: {"US-LAMP-01": 3})
        engine.bootstrap()
        engine.poll()
        engine.history.import_summaries(db)
        again = engine.history.detail(US, "2026-11-04 10:00")
        self.assertTrue(again["has_payload"])
        self.assertEqual(again["rows"][0]["afn_volume"], 3)

    def test_span_over_seven_days_is_rejected(self):
        engine, _fake, _clock, _db = make_engine()
        with self.assertRaises(ValueError):
            engine.history.captures_between("2026-11-01 00:00", "2026-11-08 00:15")


if __name__ == "__main__":
    unittest.main()
