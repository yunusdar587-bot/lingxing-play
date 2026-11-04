"""从本地库读取尺寸、映射、汇率、退货率、库龄快照。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from .db import Database
from .fields import num
from .pricing import Dims
from .timeutil import month_str, prev_month

AGE_FINE = (
    ("0-30", ("inv_age_0_to_30_days",)),
    ("31-60", ("inv_age_31_to_60_days",)),
    ("61-90", ("inv_age_61_to_90_days",)),
)
AGE_COARSE_0_90 = ("inv_age_0_to_90_days",)
AGE_REST = (
    ("91-180", ("inv_age_91_to_180_days",)),
    ("181-270", ("inv_age_181_to_270_days",)),
    ("271-365", ("inv_age_271_to_365_days",)),
    ("365+", ("inv_age_365_plus_days", "inv_age_365_plus", "inv_age_366_plus_days")),
)


# --------------------------------------------------------------------------- 映射
def listing_map(db: Database, sid: int) -> dict[str, str]:
    return {r["fnsku"]: r["msku"] for r in db.query("SELECT fnsku, msku FROM listing_map WHERE sid=?", (sid,))}


# --------------------------------------------------------------------------- 尺寸
def _dims_from_row(row: dict, month: str) -> Dims:
    return Dims(
        fnsku=str(row.get("fnsku") or ""),
        month=month,
        longest=num(row.get("longest_side")),
        median=num(row.get("median_side")),
        shortest=num(row.get("shortest_side")),
        length_unit=str(row.get("measurement_units") or ""),
        weight=num(row.get("weight")),
        weight_unit=str(row.get("weight_units") or ""),
        volume=num(row.get("item_volume")),
        volume_unit=str(row.get("volume_units") or ""),
        size_tier=str(row.get("product_size_tier") or ""),
        country_code=str(row.get("country_code") or ""),
        aqoh=num(row.get("average_quantity_on_hand")),
        fulfillment_center=str(row.get("fulfillment_center") or ""),
    )


@dataclass
class DimsSet:
    months: list[str]                          # 有数据的收费月，升序
    by_month: dict[str, dict[str, Dims]]       # month -> msku -> Dims
    unmapped: dict[str, set[str]]              # month -> 未映射 fnsku

    @property
    def latest_month(self) -> str | None:
        return self.months[-1] if self.months else None

    def current(self, msku: str) -> Dims | None:
        m = self.latest_month
        return self.by_month[m].get(msku) if m else None

    def for_month(self, msku: str, month: str) -> Dims | None:
        """取不晚于 month 的最近一版尺寸；更早没有则取最早一版。"""
        best = None
        for m in self.months:
            if msku in self.by_month[m]:
                if m <= month or best is None:
                    best = self.by_month[m][msku]
                if m > month:
                    break
        return best


def load_dims(db: Database, sid: int) -> DimsSet:
    mapping = listing_map(db, sid)
    by_month: dict[str, dict[str, Dims]] = {}
    unmapped: dict[str, set[str]] = {}
    for r in db.query("SELECT fnsku, month, row FROM storage_fee_rows WHERE sid=? ORDER BY month", (sid,)):
        month = r["month"]
        row = json.loads(r["row"])
        msku = mapping.get(r["fnsku"])
        if not msku:
            unmapped.setdefault(month, set()).add(r["fnsku"])
            continue
        d = _dims_from_row(row, month)
        cur = by_month.setdefault(month, {}).get(msku)
        # 多仓尺寸不一致时取 average_quantity_on_hand 最大的那一行
        if cur is None or d.aqoh > cur.aqoh:
            by_month[month][msku] = d
    months = sorted(set(by_month) | set(unmapped))
    for m in months:
        by_month.setdefault(m, {})
    return DimsSet(months=months, by_month=by_month, unmapped=unmapped)


# --------------------------------------------------------------------------- 汇率
@dataclass
class FxRate:
    code: str
    month: str
    rate: float
    source: str          # my_rate | rate_org
    requested_month: str

    @property
    def is_fallback(self) -> bool:
        return self.month != self.requested_month


def fx_rate(db: Database, code: str, d: date) -> FxRate | None:
    """取当月汇率；当月未发布时用最近一个已发布月份。优先 my_rate，其次 rate_org。"""
    want = month_str(d)
    for r in db.query("SELECT month, my_rate, rate_org FROM fx_rates WHERE code=? AND month<=? ORDER BY month DESC",
                      (code, want)):
        if r["my_rate"] and r["my_rate"] > 0:
            return FxRate(code, r["month"], float(r["my_rate"]), "my_rate", want)
        if r["rate_org"] and r["rate_org"] > 0:
            return FxRate(code, r["month"], float(r["rate_org"]), "rate_org", want)
    return None


def fx_months_to_try(d: date, back: int = 12) -> list[str]:
    out, cur = [], d.replace(day=1)
    for _ in range(back + 1):
        out.append(month_str(cur))
        cur = prev_month(cur)
    return out


# --------------------------------------------------------------------------- 退货率
@dataclass
class ReturnRates:
    version_id: int | None
    window_start: str | None
    window_end: str | None
    computed_at: str | None
    q: dict[str, float]
    flagged: set[str]


def load_return_rates(db: Database, sid: int) -> ReturnRates:
    v = db.one("SELECT * FROM return_rate_versions WHERE sid=? ORDER BY id DESC LIMIT 1", (sid,))
    if not v:
        return ReturnRates(None, None, None, None, {}, set())
    rows = db.query("SELECT msku, q, flagged FROM return_rates WHERE version_id=?", (v["id"],))
    return ReturnRates(
        v["id"], v["window_start"], v["window_end"], v["computed_at"],
        {r["msku"]: r["q"] for r in rows}, {r["msku"] for r in rows if r["flagged"]},
    )


# --------------------------------------------------------------------------- 库龄
@dataclass
class AgeSnapshot:
    id: int
    fetched_at: str
    snapshot_date: str | None
    by_msku: dict[str, dict[str, float]]      # msku -> bucket -> 可售件数
    unmapped_fnskus: set[str]


def age_buckets(row: dict) -> dict[str, float]:
    """互不重叠的库龄桶。有 0-30/31-60/61-90 时不再使用 0-90。"""
    out: dict[str, float] = {}
    fine_present = any(any(n in row for n in names) for _, names in AGE_FINE)
    if fine_present:
        for label, names in AGE_FINE:
            out[label] = sum(num(row.get(n)) for n in names)
    else:
        out["0-90"] = sum(num(row.get(n)) for n in AGE_COARSE_0_90)
    for label, names in AGE_REST:
        out[label] = num(next((row[n] for n in names if n in row), 0))
    return out


def load_age_snapshot(db: Database, sid: int) -> AgeSnapshot | None:
    r = db.one("SELECT * FROM age_snapshots WHERE sid=? ORDER BY id DESC LIMIT 1", (sid,))
    if not r:
        return None
    return parse_age_snapshot(db, sid, r)


def parse_age_snapshot(db: Database, sid: int, r) -> AgeSnapshot:
    mapping = listing_map(db, sid)
    by_msku: dict[str, dict[str, float]] = {}
    unmapped: set[str] = set()
    for row in json.loads(r["rows"]):
        msku = row.get("sku") or row.get("msku") or row.get("seller_sku")
        if not msku:
            msku = mapping.get(str(row.get("fnsku") or ""))
        if not msku:
            if row.get("fnsku"):
                unmapped.add(str(row["fnsku"]))
            continue
        buckets = age_buckets(row)
        agg = by_msku.setdefault(str(msku), {})
        for k, v in buckets.items():
            agg[k] = agg.get(k, 0.0) + v
    return AgeSnapshot(r["id"], r["fetched_at"], r["snapshot_date"], by_msku, unmapped)


def snapshot_date_of(rows: list[dict]) -> str | None:
    dates = sorted({str(r.get("snapshot_date"))[:10] for r in rows if r.get("snapshot_date")})
    return dates[-1] if dates else None
