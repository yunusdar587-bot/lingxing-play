import unittest
from datetime import date, datetime, timezone

from lingxing_dashboard import pricing
from lingxing_dashboard.calc import MODE_DAILY, MODE_ORDER, StoreContext, compute_rows, sign_check, summarize
from lingxing_dashboard.fields import normalize_row
from lingxing_dashboard.fifo import SoldPiece
from lingxing_dashboard.pricing import Dims
from lingxing_dashboard.refdata import AgeSnapshot, DimsSet, FxRate, ReturnRates
from lingxing_dashboard.sites import SITES

# 洛杉矶 2026-11-04 12:00（已过一半）
NOW = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)


def dims(msku="A"):
    return Dims(fnsku="F" + msku, month="2026-10", longest=12, median=12, shortest=12, length_unit="inches",
                weight=1.0, weight_unit="pounds", size_tier="Large standard-size", aqoh=10)


def ctx(profit, q=None, age=None, sold=None, t0="2026-10-01T00:00:00+00:00", with_dims=True, cost_sign="negative"):
    d = dims()
    ds = DimsSet(months=["2026-10"], by_month={"2026-10": {"A": d} if with_dims else {}}, unmapped={})
    return StoreContext(
        sid=1, name="s", site=SITES["US"], now=NOW, local_date=date(2026, 11, 4),
        profit={r["msku"]: r for r in profit},
        q=ReturnRates(1, "2025-10-05", "2026-10-05", "x", q or {}, set()),
        dims=ds, age=AgeSnapshot(1, "x", "2026-11-03", age or {}, set()),
        fx=FxRate("USD", "2026-11", 7.0, "my_rate", "2026-11"), price=pricing.get_version("2026.1"),
        sold=sold or {}, t0=t0, cost_sign=cost_sign,
    )


def prow(**kw):
    base = {"sid": 1, "msku": "A", "gross_profit": 100, "net_amount": 500, "selling_fee": -75, "afn_volume": 10,
            "refund_amount": -20, "fba_storage_fee": -5, "shared_fba_storage_fee": -1, "long_term_storage_fee": -2,
            "shared_long_term_storage_fee": 0, "shared_fba_disposal_fee": -3}
    base.update(kw)
    return normalize_row(base)


class ReturnCostTest(unittest.TestCase):
    def test_formula_with_disposal(self):
        rows = compute_rows(ctx([prow()], q={"A": 0.05}))
        r = rows[0]
        # 大号标准件按体积重：1728 in³ / 139 = 12.43 lb，超出 2 lb 部分向上取整为 11 lb
        disposal_usd = 2.89 + 1.06 * 11
        self.assertAlmostEqual(r["disposal_unit_local"], disposal_usd)
        expect = 0.05 * (500 - 0.8 * 75 + disposal_usd * 7.0 * 10)
        self.assertAlmostEqual(r["return_cost"], expect)

    def test_afn_zero_keeps_first_half(self):
        r = compute_rows(ctx([prow(afn_volume=0)], q={"A": 0.05}))[0]
        self.assertAlmostEqual(r["return_cost"], 0.05 * (500 - 60))

    def test_both_zero_is_zero(self):
        r = compute_rows(ctx([prow(afn_volume=0, net_amount=0)], q={}))[0]
        self.assertEqual((r["return_cost"], r["return_cost_status"]), (0.0, "ok"))

    def test_missing_q_not_ready_and_excluded(self):
        rows = compute_rows(ctx([prow()], q={}, age={"A": {"0-30": 10}}))
        self.assertIsNone(rows[0]["return_cost"])
        s = summarize(rows)
        self.assertEqual(s[MODE_DAILY]["excluded"], 1)
        self.assertEqual(s[MODE_DAILY]["excluded_reasons"], {"no_q": 1})
        self.assertIsNone(s[MODE_DAILY]["adjusted"])


class AdjustedProfitTest(unittest.TestCase):
    def test_daily_mode_formula_and_composition(self):
        rows = compute_rows(ctx([prow()], q={"A": 0.05}, age={"A": {"0-30": 10, "181-270": 2}}))
        r = rows[0]
        self.assertEqual(r["daily_storage_status"], "ok")
        self.assertAlmostEqual(r["elapsed_storage_cny"], r["daily_storage_cny"] * 0.5)
        expect = 100 - (-20) - (-5) - (-1) - (-2) - 0 - (-3) - r["return_cost"] - r["elapsed_storage_cny"]
        self.assertAlmostEqual(r["adjusted"][MODE_DAILY], expect)
        s = summarize(rows)[MODE_DAILY]
        self.assertAlmostEqual(s["steps_total"], s["adjusted"])

    def test_daily_storage_formula(self):
        rows = compute_rows(ctx([prow()], q={"A": 0.05}, age={"A": {"0-30": 10, "181-270": 2, "365+": 1}}))
        vol_ft3 = 1.0  # 12x12x12 in
        base = 2.40 * vol_ft3
        expect = (10 * base + 2 * (base + 0.50) + 1 * (base + max(6.90 * vol_ft3, 0.30))) / 30 * 7.0
        self.assertAlmostEqual(rows[0]["daily_storage_cny"], expect)

    def test_no_age_not_ready(self):
        rows = compute_rows(ctx([prow()], q={"A": 0.05}, age={}))
        self.assertEqual(rows[0]["daily_storage_status"], "no_age")
        self.assertIsNone(rows[0]["adjusted"][MODE_DAILY])

    def test_inventory_only_msku_counts_storage(self):
        c = ctx([], q={}, age={"A": {"0-30": 30}})
        rows = compute_rows(c)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["adjusted"][MODE_DAILY], -rows[0]["elapsed_storage_cny"])

    def test_order_mode_unknown_lot_not_ready(self):
        sold = {"A": [SoldPiece(3, "opening", None, NOW.isoformat()), SoldPiece(2, "received", date(2026, 10, 1),
                                                                          NOW.isoformat())]}
        rows = compute_rows(ctx([prow()], q={"A": 0.05}, age={"A": {"0-30": 1}}, sold=sold))
        r = rows[0]
        self.assertEqual(r["order_storage_status"], "unknown_lot")
        self.assertEqual((r["order_known_units"], r["order_unknown_units"]), (2, 3))
        s = summarize(rows)
        self.assertTrue(s[MODE_ORDER]["fallback_to_daily"])
        self.assertEqual(s[MODE_ORDER]["adjusted"], s[MODE_DAILY]["adjusted"])

    def test_order_mode_known_lot_day_by_day(self):
        sold = {"A": [SoldPiece(2, "received", date(2026, 11, 2), NOW.isoformat())]}
        rows = compute_rows(ctx([prow()], q={"A": 0.05}, age={"A": {"0-30": 1}}, sold=sold))
        r = rows[0]
        self.assertEqual(r["order_storage_status"], "ok")
        # 11-02、11-03 全天 + 11-04 半天，11 月标准件 $2.40/ft³，体积 1 ft³
        expect = 2 * 2.40 / 30 * 2.5 * 7.0
        self.assertAlmostEqual(r["order_storage_cny"], expect)
        self.assertAlmostEqual(r["adjusted"][MODE_ORDER],
                               100 + 31 - r["return_cost"] - expect)


class SignCheckTest(unittest.TestCase):
    def test_pass_with_negative_costs(self):
        rows = compute_rows(ctx([prow()], q={"A": 0.05}, age={"A": {"0-30": 1}}))
        res = sign_check(1, rows, "negative")
        self.assertEqual(res["status"], "passed")
        self.assertAlmostEqual(res["change"], 27)

    def test_fail_when_costs_positive(self):
        rows = compute_rows(ctx([prow(refund_amount=20, fba_storage_fee=5, long_term_storage_fee=2)],
                                q={"A": 0.05}, age={"A": {"0-30": 1}}))
        res = sign_check(1, rows, "negative")
        self.assertEqual(res["status"], "failed")
        self.assertFalse(res["field_signs"]["refund_amount"]["ok"])

    def test_positive_convention(self):
        rows = compute_rows(ctx([prow(refund_amount=20, fba_storage_fee=5, long_term_storage_fee=2)],
                                q={"A": 0.05}, age={"A": {"0-30": 1}}, cost_sign="positive"))
        self.assertEqual(sign_check(1, rows, "positive")["status"], "passed")

    def test_no_sample(self):
        rows = compute_rows(ctx([prow(refund_amount=0, fba_storage_fee=0, long_term_storage_fee=0)], q={"A": 0.05}))
        self.assertIsNone(sign_check(1, rows, "negative"))


if __name__ == "__main__":
    unittest.main()
