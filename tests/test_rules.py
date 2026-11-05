import unittest
from datetime import date

from lingxing_dashboard import pricing
from lingxing_dashboard.fields import aggregate, normalize_row, parse_return_rate
from lingxing_dashboard.pricing import Dims
from lingxing_dashboard.refdata import age_buckets


class ReturnRateTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_return_rate("5%"), (0.05, False))
        self.assertEqual(parse_return_rate("0.05"), (0.05, False))
        self.assertEqual(parse_return_rate(0.05), (0.05, False))
        q, flagged = parse_return_rate("7.8")
        self.assertAlmostEqual(q, 0.078)
        self.assertTrue(flagged)
        self.assertEqual(parse_return_rate(1), (1.0, False))
        self.assertEqual(parse_return_rate(None), (None, False))
        self.assertEqual(parse_return_rate(""), (None, False))


class FieldsTest(unittest.TestCase):
    def test_nested_msku_and_sid(self):
        row = normalize_row({
            "sids": ["1510"],
            "price_list": [{"seller_sku": "SKU-1", "asin": "B001", "local_name": "品"}],
            "net_amount": "12.5",
            "afn_volume": 3,
            "gross_profit": "1.2",
        })
        self.assertIsNotNone(row)
        self.assertEqual(row["sid"], 1510)
        self.assertEqual(row["msku"], "SKU-1")
        self.assertEqual(row["asin"], "B001")
        self.assertEqual(row["local_name"], "品")
        self.assertEqual(row["afn_volume"], 3)

    def test_aggregate_same_key_sums(self):
        rows = [normalize_row({"sid": 1, "msku": "A", "net_amount": "10", "afn_volume": 2}),
                normalize_row({"sid": 1, "msku": "A", "net_amount": 5, "afn_volume": 1})]
        agg = aggregate(rows)
        self.assertEqual(agg[(1, "A")]["net_amount"], 15)
        self.assertEqual(agg[(1, "A")]["afn_volume"], 3)


class AgeBucketTest(unittest.TestCase):
    def test_no_double_count_0_90(self):
        row = {"inv_age_0_to_30_days": 1, "inv_age_31_to_60_days": 2, "inv_age_61_to_90_days": 3,
               "inv_age_0_to_90_days": 6, "inv_age_91_to_180_days": 4, "inv_age_365_plus_days": 1}
        b = age_buckets(row)
        self.assertNotIn("0-90", b)
        self.assertEqual(sum(b.values()), 11)

    def test_coarse_only(self):
        b = age_buckets({"inv_age_0_to_90_days": 6, "inv_age_91_to_180_days": 4})
        self.assertEqual(b["0-90"], 6)
        self.assertEqual(sum(b.values()), 10)


def us_dims(l, w, h, lb, tier):
    return Dims(longest=l, median=w, shortest=h, length_unit="inches", weight=lb, weight_unit="pounds",
                size_tier=tier)


class PricingTest(unittest.TestCase):
    def setUp(self):
        self.v2025 = pricing.get_version("2025.1")
        self.v2026 = pricing.get_version("2026.1")

    def test_versions_have_four_countries(self):
        for t in pricing.versions():
            self.assertEqual(set(t["countries"]), {"US", "CA", "MX", "BR"})
            self.assertTrue(t["effective_date"])

    def test_us_disposal_small_standard_by_version(self):
        d = us_dims(10, 6, 0.5, 0.3, "Small standard-size")
        self.assertEqual(pricing.disposal_fee_local(self.v2025, "US", d)[0], 1.04)
        self.assertEqual(pricing.disposal_fee_local(self.v2026, "US", d)[0], 0.84)

    def test_us_disposal_large_standard_per_lb(self):
        d = us_dims(12, 10, 4, 3.2, "Large standard-size")
        fee, _ = pricing.disposal_fee_local(self.v2026, "US", d)
        self.assertAlmostEqual(fee, 2.89 + 1.06 * 2)

    def test_us_disposal_uses_dimensional_weight_for_large_standard(self):
        # 18x14x8 = 2016 in³ / 139 = 14.5 lb 体积重 > 1 lb 实重
        d = us_dims(18, 14, 8, 1.0, "Large standard-size")
        fee, _ = pricing.disposal_fee_local(self.v2026, "US", d)
        self.assertAlmostEqual(fee, 2.89 + 1.06 * 13)

    def test_br_disposal_dim_weight(self):
        d = Dims(longest=30, median=20, shortest=10, length_unit="cm", weight=0.3, weight_unit="kg")
        # 6000 cm³ / 6000 = 1.0 kg > 0.3 kg
        self.assertEqual(pricing.disposal_fee_local(self.v2026, "BR", d)[0], 1.12)

    def test_storage_rates_season_and_class(self):
        d = us_dims(10, 8, 4, 1, "Large standard-size")
        self.assertEqual(pricing.base_storage_rate(self.v2026, "US", d, date(2026, 3, 1))[0], 0.78)
        self.assertEqual(pricing.base_storage_rate(self.v2026, "US", d, date(2026, 11, 1))[0], 2.40)
        big = Dims(longest=30, median=30, shortest=20, length_unit="cm", weight=1, weight_unit="kg")
        self.assertEqual(pricing.base_storage_rate(self.v2026, "BR", big, date(2026, 11, 1))[0], 37.5)

    def test_365_plus_uses_first_tier_above_365(self):
        row = pricing.aged_row_for_bucket(self.v2026, "US", 366)
        self.assertEqual((row["min_days"], row["rate"], row["min_per_unit"]), (366, 6.90, 0.30))
        self.assertIsNone(pricing.aged_row_for_bucket(self.v2026, "BR", 181))

    def test_monthly_fee_min_per_unit(self):
        tiny = us_dims(2, 2, 1, 0.1, "Small standard-size")  # 4 in³
        row = pricing.aged_row_for_bucket(self.v2026, "US", 366)
        fee = pricing.monthly_unit_fee(self.v2026, "US", tiny, date(2026, 11, 1), row)
        vol_ft3 = 4 / 1728
        self.assertAlmostEqual(fee, 2.40 * vol_ft3 + 0.30, places=6)


if __name__ == "__main__":
    unittest.main()
