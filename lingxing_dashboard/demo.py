"""演示 / 测试用的假领星后端。按真实路径与参数返回数据，并校验签名。"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .lingxing.client import make_sign
from .sites import SITES
from .timeutil import local_now, utcnow

DEMO_APP_ID = "demo_app_id_0001"   # 16 字节，可直接作为 AES-128 密钥
DEMO_SECRET = "demo-secret"

STORES = [
    {"sid": 101, "name": "US-主店", "country": "美国", "marketplace_id": "ATVPDKIKX0DER", "region": "NA"},
    {"sid": 102, "name": "CA-主店", "country": "加拿大", "marketplace_id": "A2EUQ1WTGCTBG2", "region": "NA"},
    {"sid": 103, "name": "MX-主店", "country": "墨西哥", "marketplace_id": "A1AM78C64UM0Y8", "region": "NA"},
    {"sid": 104, "name": "BR-主店", "country": "巴西", "marketplace_id": "A2Q3Y263D00KWC", "region": "SA"},
    {"sid": 201, "name": "UK-店铺（应被过滤）", "country": "英国", "marketplace_id": "A1F83G8C2ARO7P", "region": "EU"},
]
SID_SITE = {101: "US", 102: "CA", 103: "MX", 104: "BR"}

UNITS = {
    "US": ("inches", "pounds", "cubic feet", 2.54, 0.4536, 28316.85),
    "CA": ("centimeters", "kilograms", "cubic meters", 1.0, 1.0, 1e6),
    "MX": ("centimeters", "kilograms", "cubic meters", 1.0, 1.0, 1e6),
    "BR": ("centimeters", "kilograms", "cubic meters", 1.0, 1.0, 1e6),
}

# (msku 后缀, 每日销量, 单价 CNY, 边长 cm, 重量 kg, 尺寸档, 退货率原值)
PRODUCTS = [
    ("LAMP-01", 40, 180.0, (30, 20, 10), 0.9, "Large standard-size", "4.5%"),
    ("CABLE-02", 120, 45.0, (15, 10, 1.5), 0.08, "Small standard-size", "0.021"),
    ("CHAIR-03", 6, 900.0, (65, 45, 30), 9.0, "Large bulky", "7.8"),
    ("MUG-04", 25, 95.0, (14, 12, 12), 0.45, "Large standard-size", "3%"),
    ("NEW-05", 8, 120.0, (25, 18, 6), 0.5, "Large standard-size", None),
    ("TENT-06", 3, 650.0, (60, 30, 30), 6.5, "Large standard-size", "12%"),
]


def _seed(*parts) -> random.Random:
    return random.Random(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest())


class FakeLingxing:
    def __init__(self, clock: Callable[[], datetime] = utcnow, storage_report_ready: dict[int, bool] | None = None,
                 cost_sign: str = "negative"):
        self.clock = clock
        self.calls: list[tuple[str, dict]] = []
        self.storage_report_ready = storage_report_ready or {104: False}
        self.empty_storage_months: dict[int, set[str]] = {}
        self.cost_sign = cost_sign
        self.receipts: dict[int, list[dict]] = {}
        self.age_override: dict[int, list[dict]] = {}
        self.profit_override: Callable[[list[int], str, str], list[dict] | None] | None = None
        self.token_seq = 0
        self.rate_limit_next = 0

    # ---- transport ----
    def __call__(self, method: str, url: str, body: bytes | None, headers: dict) -> tuple[int, bytes]:
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/auth-server/oauth/access-token":
            if q.get("appId") != DEMO_APP_ID or q.get("appSecret") != DEMO_SECRET:
                return 200, json.dumps({"code": "400", "msg": "bad app"}).encode()
            self.token_seq += 1
            return 200, json.dumps({"code": "200", "msg": "OK", "data": {
                "access_token": f"tok-{self.token_seq}", "refresh_token": "ref", "expires_in": 7199}}).encode()
        if u.path == "/api/auth-server/oauth/refresh":
            self.token_seq += 1
            return 200, json.dumps({"code": "200", "msg": "OK", "data": {
                "access_token": f"tok-{self.token_seq}", "refresh_token": "ref", "expires_in": 7199}}).encode()
        params = json.loads(body.decode()) if body else {k: v for k, v in q.items()
                                                         if k not in ("access_token", "app_key", "timestamp", "sign")}
        if method == "GET":
            for k, v in list(params.items()):
                if isinstance(v, str) and v[:1] in "[{":
                    params[k] = json.loads(v)
        sign_params = {**params, "access_token": q.get("access_token"), "app_key": q.get("app_key"),
                       "timestamp": q.get("timestamp")}
        if make_sign(DEMO_APP_ID, sign_params) != q.get("sign"):
            return 200, json.dumps({"code": 2001006, "message": "sign error"}).encode()
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return 200, json.dumps({"code": 3001008, "message": "请求太频繁"}).encode()
        self.calls.append((u.path, params))
        handler = {
            "/erp/sc/data/seller/lists": self._sellers,
            "/basicOpen/finance/mreport/OrderProfit": self._order_profit,
            "/erp/sc/routing/fba/fbaStock/getFbaAgeList": self._age,
            "/erp/sc/data/fba_report/receivedInventory": self._received,
            "/erp/sc/data/fba_report/storageFeeMonth": self._storage_month,
            "/erp/sc/data/mws/listing": self._listing,
            "/erp/sc/routing/finance/currency/currencyMonth": self._currency,
        }.get(u.path)
        if handler is None:
            return 404, b'{"code": 404, "message": "not found"}'
        data = handler(params)
        if isinstance(data, tuple):
            data, total = data
        else:
            total = len(data)
        offset, length = int(params.get("offset", 0)), int(params.get("length", 1000))
        page = data[offset: offset + length] if "offset" in params else data
        return 200, json.dumps({"code": 0, "message": "success", "data": page, "total": total},
                               ensure_ascii=False).encode()

    # ---- data ----
    def _sellers(self, _p):
        return STORES

    @staticmethod
    def msku(sid: int, suffix: str) -> str:
        return f"{SID_SITE[sid]}-{suffix}"

    @staticmethod
    def fnsku(sid: int, suffix: str) -> str:
        return "X00" + hashlib.md5(f"{sid}{suffix}".encode()).hexdigest()[:7].upper()

    def _products(self, sid: int):
        prods = PRODUCTS if sid != 104 else PRODUCTS[:4]
        return prods

    def _order_profit(self, p):
        sids = [int(s) for s in p["sids"]]
        start, end = p["startDate"], p["endDate"]
        if self.profit_override:
            res = self.profit_override(sids, start, end)
            if res is not None:
                return res
        rows = []
        long_window = (datetime.fromisoformat(end[:10]) - datetime.fromisoformat(start[:10])).days > 31
        for sid in sids:
            if sid not in SID_SITE:
                continue
            for suffix, per_day, price, *_rest, rr in self._products(sid):
                msku = self.msku(sid, suffix)
                if long_window:
                    if rr is None:
                        continue
                    rows.append({"sid": sid, "msku": msku, "return_rate": rr, "afn_volume": per_day * 365})
                    continue
                end_dt = datetime.fromisoformat(end) if len(end) > 10 else datetime.fromisoformat(end + " 23:59:59")
                frac = (end_dt.hour * 3600 + end_dt.minute * 60 + end_dt.second) / 86400
                rnd = _seed(sid, suffix, end[:10])
                wobble = 0.85 + 0.3 * rnd.random()
                units = int(per_day * wobble * frac)
                net = round(units * price, 2)
                sign = -1 if self.cost_sign == "negative" else 1
                sell = round(net * 0.15, 2)
                fba = round(units * price * 0.12, 2)
                ads = round(net * 0.08, 2)
                cg = round(units * price * 0.35, 2)
                head = round(units * price * 0.06, 2)
                refund = round(net * 0.03, 2) if units else 0.0
                storage = round(per_day * 0.6, 2) if suffix != "CABLE-02" else 0.0
                lt = round(per_day * 0.2, 2) if suffix in ("TENT-06", "CHAIR-03") else 0.0
                disposal = 3.5 if suffix == "CHAIR-03" else 0.0
                costs = sell + fba + ads + cg + head + refund + storage + lt + disposal
                rows.append({
                    "sid": sid, "msku": msku, "asin": "B0" + msku[-6:].replace("-", ""),
                    "fnsku": self.fnsku(sid, suffix), "local_name": suffix.split("-")[0].title(),
                    "afn_volume": units, "net_amount": f"{net:.2f}",
                    "selling_fee": sign * sell, "fba_delivery_fee": sign * fba, "total_ads_cost": sign * ads,
                    "cg_price_total": sign * cg, "cg_transport_costs_total": sign * head,
                    "refund_amount": sign * refund, "fba_storage_fee": sign * storage,
                    "shared_fba_storage_fee": sign * round(storage * 0.1, 2),
                    "long_term_storage_fee": sign * lt, "shared_long_term_storage_fee": 0,
                    "shared_fba_disposal_fee": sign * disposal,
                    "gross_profit": round(net - costs - round(storage * 0.1, 2), 2),
                    "return_rate": rr or "",
                })
        return rows

    def _age(self, p):
        sid = int(p["sid"])
        if sid in self.age_override:
            return self.age_override[sid]
        site = SITES[SID_SITE[sid]]
        today = local_now(site.tz, self.clock()).date()
        snap = today - timedelta(days=4 if sid == 103 else 1)
        rows = []
        for suffix, per_day, *_ in self._products(sid):
            rnd = _seed("age", sid, suffix)
            stock = per_day * 45
            b = [int(stock * w) for w in (0.30, 0.22, 0.15, 0.18, 0.08, 0.05, 0.02)]
            if suffix == "NEW-05":
                b = [stock, 0, 0, 0, 0, 0, 0]
            rows.append({
                "sid": sid, "sku": self.msku(sid, suffix), "fnsku": self.fnsku(sid, suffix),
                "snapshot_date": snap.isoformat(),
                "inv_age_0_to_30_days": b[0], "inv_age_31_to_60_days": b[1], "inv_age_61_to_90_days": b[2],
                "inv_age_0_to_90_days": b[0] + b[1] + b[2],
                "inv_age_91_to_180_days": b[3], "inv_age_181_to_270_days": b[4],
                "inv_age_271_to_365_days": b[5], "inv_age_365_plus_days": b[6],
                "_noise": rnd.random(),
            })
        rows.append({"sid": sid, "sku": "", "fnsku": "X00UNMAPPED", "snapshot_date": snap.isoformat(),
                     "inv_age_0_to_30_days": 5})
        return rows

    def _received(self, p):
        sid = int(p["sid"])
        lo, hi = p["start_date"], p["end_date"]
        return [r for r in self.receipts.get(sid, []) if lo <= str(r["received_date_report"])[:10] <= hi]

    def _storage_month(self, p):
        sid = int(p["sid"])
        if not self.storage_report_ready.get(sid, True):
            return []
        if p.get("month") in self.empty_storage_months.get(sid, ()):
            return []
        lu, wu, vu, lf, wf, vf = UNITS[SID_SITE[sid]]
        rows = []
        for suffix, per_day, _price, sides, kg, tier, _rr in self._products(sid):
            if suffix == "NEW-05":
                continue
            for fc, aqoh, scale in (("FC1", per_day * 30, 1.0), ("FC2", per_day * 5, 1.1)):
                s = [x * scale / lf for x in sides]
                vol_cm3 = sides[0] * sides[1] * sides[2] * scale ** 3
                rows.append({
                    "sid": sid, "fnsku": self.fnsku(sid, suffix), "asin": "", "fulfillment_center": fc,
                    "country_code": SID_SITE[sid], "longest_side": round(s[0], 2), "median_side": round(s[1], 2),
                    "shortest_side": round(s[2], 2), "measurement_units": lu, "weight": round(kg * scale / wf, 3),
                    "weight_units": wu, "item_volume": round(vol_cm3 / vf, 6), "volume_units": vu,
                    "product_size_tier": tier, "average_quantity_on_hand": aqoh, "month_of_charge": p["month"],
                })
        rows.append({"sid": sid, "fnsku": "X00UNMAPPED", "fulfillment_center": "FC1", "longest_side": 10,
                     "median_side": 5, "shortest_side": 2, "measurement_units": lu, "weight": 0.2, "weight_units": wu,
                     "item_volume": 0.001, "volume_units": vu, "product_size_tier": "Small standard-size",
                     "average_quantity_on_hand": 10, "month_of_charge": p["month"]})
        return rows

    def _listing(self, p):
        sid = int(p["sid"])
        return [{"sid": sid, "seller_sku": self.msku(sid, s[0]), "fnsku": self.fnsku(sid, s[0])}
                for s in self._products(sid)]

    def _currency(self, p):
        month = p["date"]
        now_month = self.clock().strftime("%Y-%m")
        if month == now_month:
            return [{"date": month, "code": "USD", "rate_org": 7.12, "my_rate": 7.10}]
        return [
            {"date": month, "code": "USD", "rate_org": 7.15, "my_rate": 7.13},
            {"date": month, "code": "CAD", "rate_org": 5.18, "my_rate": 0},
            {"date": month, "code": "MXN", "rate_org": 0.385, "my_rate": None},
            {"date": month, "code": "BRL", "rate_org": 1.29, "my_rate": 1.28},
        ]

    def add_receipt(self, sid: int, suffix: str, day: date, qty: int) -> None:
        self.receipts.setdefault(sid, []).append({
            "sid": sid, "sku": self.msku(sid, suffix), "fnsku": self.fnsku(sid, suffix),
            "received_date_report": day.isoformat(), "quantity": qty,
            "fba_shipment_id": f"FBA{len(self.receipts.get(sid, []))}", "fulfillment_center_id": "FC1",
        })
