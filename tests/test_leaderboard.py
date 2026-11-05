import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from lingxing_dashboard.leaderboard import CONFIG_KEY, ConfigError, LeaderboardService
from lingxing_dashboard.web import DashboardService, make_handler

from tests.helpers import make_engine

US, CA, MX, BR = 101, 102, 103, 104


def payload(svc, mode="daily", columns=None, q=None, **kw):
    cols = columns or [{"name": "北美", "sids": [US, CA]}, {"name": "拉美", "sids": [MX]}, {"name": "巴西", "sids": [BR]}]
    sel = {s for c in cols for s in c["sids"]}
    need = [r["key"] for r in svc.review_lists()["q_missing"] if r["sid"] in sel]
    q = q if q is not None else {k: {"kind": "zero"} for k in need}
    body = {"mode": mode, "columns": cols, "reviewed_dims": True, "reviewed_unknown_lots": True,
            "q_reviewed": need, "q_overrides": q}
    body.update(kw)
    return body


class LeaderboardTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.fake, self.clock, self.db = make_engine(datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc))
        self.engine.bootstrap()
        self.engine.poll()
        self.svc = LeaderboardService(self.engine)

    def snapshot_rows(self, sid):
        snap = self.svc.latest_today(sid, self.engine.store(sid)["site"].code)
        return json.loads(snap["rows"])

    # ---------------------------------------------------------------- 配置校验
    def test_config_validation(self):
        bad = [
            payload(self.svc, mode="x"),
            payload(self.svc, columns=[{"name": "a", "sids": [US]}, {"name": "b", "sids": []}]),
            payload(self.svc, columns=[{"name": " ", "sids": [US]}, {"name": "b", "sids": []}, {"name": "c", "sids": []}]),
            payload(self.svc, columns=[{"name": "a", "sids": [US]}, {"name": "b", "sids": [US]}, {"name": "c", "sids": []}]),
            payload(self.svc, columns=[{"name": "a", "sids": [999]}, {"name": "b", "sids": []}, {"name": "c", "sids": []}]),
            payload(self.svc, reviewed_dims=False),
            payload(self.svc, reviewed_unknown_lots=False),
            payload(self.svc, q_reviewed=[]),
        ]
        for body in bad:
            with self.assertRaises(ConfigError):
                self.svc.save_config(body)
        self.assertIsNone(self.db.get(CONFIG_KEY))

    def test_q_requires_valid_choice_for_selected_stores_only(self):
        need = [r for r in self.svc.review_lists()["q_missing"]]
        self.assertTrue(need)
        key = need[0]["key"]
        body = payload(self.svc, q={**{r["key"]: {"kind": "zero"} for r in need}, key: {"kind": "pct", "pct": 150}})
        with self.assertRaises(ConfigError):
            self.svc.save_config(body)
        # 缺 Q 的店铺不选入任何列时，不需要处理
        others = [s["sid"] for s in self.engine.stores() if s["sid"] not in {r["sid"] for r in need}]
        cols = [{"name": "a", "sids": others}, {"name": "b", "sids": []}, {"name": "c", "sids": []}]
        self.svc.save_config(payload(self.svc, columns=cols, q={}))
        self.assertEqual(self.db.get(CONFIG_KEY)["columns"][0]["sids"], others)

    def test_not_ready_store_blocks_confirm(self):
        with self.db.tx() as c:
            c.execute("DELETE FROM snapshots WHERE sid=?", (BR,))
        ready = {s["sid"]: s for s in self.svc.readiness()}
        self.assertFalse(ready[BR]["ready"])
        self.assertIn("轮询", ready[BR]["waiting_for"])
        with self.assertRaises(ConfigError):
            self.svc.save_config(payload(self.svc))

    # ---------------------------------------------------------------- 退货率
    def test_manual_q_applies_through_existing_formula(self):
        item = self.svc.review_lists()["q_missing"][0]
        sid, msku = item["sid"], item["msku"]
        row = next(r for r in self.snapshot_rows(sid) if r["msku"] == msku)
        self.assertIsNone(row["adjusted"]["daily"])

        need = [r["key"] for r in self.svc.review_lists()["q_missing"]]
        q = {k: {"kind": "zero"} for k in need}
        q[item["key"]] = {"kind": "pct", "pct": 10}
        self.svc.save_config(payload(self.svc, q=q))
        row = next(r for r in self.snapshot_rows(sid) if r["msku"] == msku)
        self.assertEqual(row["q_source"], "manual_pct")
        self.assertAlmostEqual(row["q"], 0.10)
        expect = 0.10 * (row["net_amount"] - 0.8 * abs(row["selling_fee"])
                         + row["disposal_unit_cny"] * row["afn_volume"])
        self.assertAlmostEqual(row["return_cost"], expect, places=6)
        review = {r["key"]: r for r in self.svc.review_lists()["q_missing"]}
        self.assertEqual(review[item["key"]]["current"]["kind"], "pct")

        q[item["key"]] = {"kind": "zero"}
        self.svc.save_config(payload(self.svc, q=q))
        row = next(r for r in self.snapshot_rows(sid) if r["msku"] == msku)
        self.assertEqual(row["q_source"], "manual_zero")
        self.assertEqual(row["return_cost"], 0)

    # ---------------------------------------------------------------- 排行榜
    def test_totals_sorting_and_membership(self):
        self.svc.save_config(payload(self.svc))
        board = self.svc.leaderboard()
        self.assertEqual([c["name"] for c in board["columns"]], ["北美", "拉美", "巴西"])
        col = board["columns"][0]
        expected = []
        for sid in (US, CA):
            for r in self.snapshot_rows(sid):
                if r["adjusted"]["daily"] is not None:
                    expected.append(r)
        self.assertEqual(len(col["rows"]), len(expected))
        self.assertAlmostEqual(col["total"]["afn_volume"], sum(r["afn_volume"] for r in expected))
        profit = sum(r["adjusted"]["daily"] for r in expected)
        self.assertAlmostEqual(col["total"]["adjusted"], profit)
        self.assertAlmostEqual(col["total"]["margin"], profit / sum(r["net_amount"] for r in expected))
        keys = [(-r["adjusted"], -r["afn_volume"], r["store"], r["msku"]) for r in col["rows"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(set(col["rows"][0]), {"store", "msku", "afn_volume", "adjusted", "margin"})
        stores = {r["store"] for c in board["columns"] for r in c["rows"]}
        self.assertTrue(stores <= {"US-主店", "CA-主店", "MX-主店", "BR-主店"})

    def test_store_outside_columns_not_shown(self):
        cols = [{"name": "a", "sids": [US]}, {"name": "b", "sids": []}, {"name": "c", "sids": []}]
        self.svc.save_config(payload(self.svc, columns=cols))
        board = self.svc.leaderboard()
        self.assertEqual({r["store"] for r in board["columns"][0]["rows"]}, {"US-主店"})
        self.assertEqual(board["columns"][1]["rows"], [])
        self.assertEqual(board["columns"][1]["total"]["afn_volume"], 0)
        self.assertIsNone(board["columns"][1]["total"]["margin"])

    def test_order_mode_excludes_unknown_lots_without_fallback(self):
        self.svc.save_config(payload(self.svc, mode="order"))
        board = self.svc.leaderboard()
        unknown = {(r["store"], r["msku"]) for r in self.svc.review_lists()["unknown_lots"]}
        self.assertTrue(unknown)
        shown = {(r["store"], r["msku"]) for c in board["columns"] for r in c["rows"]}
        self.assertFalse(shown & unknown)
        for sid in (US, CA, MX, BR):
            for r in self.snapshot_rows(sid):
                if r["adjusted"]["order"] is None:
                    self.assertNotIn((self.engine.store(sid)["name"], r["msku"]), shown)

    def test_sign_failed_hides_profit_and_margin(self):
        self.svc.save_config(payload(self.svc))
        self.db.set("sign_check", {"status": "failed"})
        board = self.svc.leaderboard()
        self.assertTrue(board["adjusted_hidden"])
        for c in board["columns"]:
            self.assertIsNone(c["total"]["adjusted"])
            self.assertIsNone(c["total"]["margin"])
            for r in c["rows"]:
                self.assertIsNone(r["adjusted"])
                self.assertIsNone(r["margin"])
            vols = [r["afn_volume"] for r in c["rows"]]
            self.assertEqual(vols, sorted(vols, reverse=True))

    def test_day_rollover_shows_waiting(self):
        self.svc.save_config(payload(self.svc))
        with self.db.tx() as c:
            c.execute("DELETE FROM snapshots WHERE sid=?", (BR,))
        board = self.svc.leaderboard()
        self.assertEqual([w["store"] for w in board["waiting"]], ["BR-主店"])
        self.assertEqual(board["columns"][2]["rows"], [])

    def test_leaderboard_requires_config(self):
        with self.assertRaises(ConfigError):
            self.svc.leaderboard()


class LeaderboardHttpTest(unittest.TestCase):
    def setUp(self):
        self.engine, *_ = make_engine(datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc))
        self.engine.bootstrap()
        self.engine.poll()
        self.service = DashboardService(self.engine)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        self.assertEqual(self.call("/api/leaderboard")[0], 409)
        status, page = self.call("/api/config")
        self.assertEqual(status, 200)
        self.assertIsNone(page["saved"])
        self.assertEqual(self.call("/api/config", {"mode": "daily"})[0], 400)
        status, _ = self.call("/api/config", payload(self.service.board))
        self.assertEqual(status, 200)
        status, board = self.call("/api/leaderboard")
        self.assertEqual(status, 200)
        self.assertEqual(len(board["columns"]), 3)
        self.assertEqual(self.call("/api/config")[1]["saved"]["mode"], "daily")
        self.assertEqual(self.call("/api/other", {})[0], 405)


if __name__ == "__main__":
    unittest.main()
