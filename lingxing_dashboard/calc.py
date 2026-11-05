"""服务端计算：预计退货成本、两种仓储费、调整后毛利、店铺汇总、符号验收。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from . import pricing
from .fields import REPLACED_FIELDS, RETAINED_FIELDS
from .fifo import SoldPiece
from .refdata import AgeSnapshot, AgeVolume, DimsSet, FxRate, ReturnRates
from .sites import Site
from .timeutil import days_in_month, elapsed_fraction, from_iso, month_str

BUCKET_MIN_DAYS = {"0-30": 0, "31-60": 31, "61-90": 61, "0-90": 0, "91-180": 91, "181-270": 181,
                   "271-365": 271, "365+": 366}

MODE_DAILY = "daily"
MODE_ORDER = "order"
MODE_LABELS = {MODE_DAILY: "今日已过时段的在库仓储", MODE_ORDER: "今日已售出件数的在仓仓储"}

ADDBACK_LABELS = {
    "refund_amount": "加回原退款",
    "fba_storage_fee": "加回原月仓储",
    "shared_fba_storage_fee": "加回原月仓储差异",
    "long_term_storage_fee": "加回原长期仓储",
    "shared_long_term_storage_fee": "加回原长期仓储差异",
    "shared_fba_disposal_fee": "加回原弃置",
}

PRIMARY_SIGN_FIELDS = ("refund_amount", "fba_storage_fee", "long_term_storage_fee")


def addback(value: float, cost_sign: str) -> float:
    """把一个已含在 gross_profit 里的原值剔除后，对毛利的影响。"""
    return -value if cost_sign == "negative" else value


@dataclass
class StoreContext:
    sid: int
    name: str
    site: Site
    now: datetime
    local_date: date
    profit: dict[str, dict]
    q: ReturnRates
    dims: DimsSet
    age: AgeSnapshot | None
    fx: FxRate | None
    price: dict
    sold: dict[str, list[SoldPiece]]
    t0: str | None
    cost_sign: str = "negative"
    listing_dims: dict[str, pricing.Dims] = field(default_factory=dict)
    storage_report_pending: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def country(self) -> str:
        return self.site.code

    @property
    def elapsed(self) -> float:
        return elapsed_fraction(self.site.tz, self.now)


def _zero_profit(msku: str, sid: int) -> dict:
    row = {"sid": sid, "msku": msku, "fnsku": "", "asin": "", "local_name": "", "return_rate_raw": None,
           "gross_profit": 0.0, "net_amount": 0.0, "selling_fee": 0.0, "afn_volume": 0.0, "missing_fields": [],
           "in_order_profit": False}
    for f in REPLACED_FIELDS:
        row[f] = 0.0
    for f in RETAINED_FIELDS:
        row[f] = None
    return row


def storage_dims(ctx: StoreContext, msku: str, base: pricing.Dims | None) -> pricing.Dims | None:
    """月仓储费报告有体积时用它，库龄体积只覆盖这一档的体积数字。

    报告里查不到体积时用 Listing 的包装长宽高和重量，不再用库龄体积补。
    """
    if base is not None and base.volume_cm3():
        vol: AgeVolume | None = ctx.age.volumes.get(msku) if ctx.age else None
        if vol is not None and vol.volume > 0 and pricing.volume_factor(vol.unit) is not None:
            return replace(base, volume=vol.volume, volume_unit=vol.unit)
        return base
    listed = ctx.listing_dims.get(msku)
    if listed is not None and listed.volume_cm3():
        return listed
    return None


def daily_storage_local(ctx: StoreContext, buckets: dict[str, float], dims: pricing.Dims | None,
                        d: date) -> tuple[float | None, str | None, list[dict]]:
    total_qty = sum(buckets.values())
    if total_qty <= 0:
        return 0.0, None, []
    if dims is None or not dims.volume_cm3():
        return None, "no_volume", []
    dim = days_in_month(d)
    total = 0.0
    detail = []
    for label, qty in buckets.items():
        if qty <= 0:
            continue
        mind = BUCKET_MIN_DAYS[label]
        aged = pricing.aged_row_for_bucket(ctx.price, ctx.country, mind) if mind >= 181 else None
        unit = pricing.monthly_unit_fee(ctx.price, ctx.country, dims, d, aged)
        if unit is None:
            return None, "no_rate", []
        amount = qty * unit / dim
        total += amount
        detail.append({"bucket": label, "qty": qty, "unit_month_fee": round(unit, 6), "amount_local": amount})
    return total, None, detail


def order_storage_local(ctx: StoreContext, msku: str, pieces: list[SoldPiece]) -> tuple[float | None, str | None]:
    """已知批次上每件：签收日到卖出时刻逐日计（卖出日按已过秒数/86400），再相加。"""
    total = 0.0
    for p in pieces:
        if p.kind != "received" or p.receive_date is None:
            return None, "unknown_lot"
        observed = from_iso(p.observed_at)
        sale_local = observed.astimezone(ctx.site.tz)
        sale_day = sale_local.date()
        frac = elapsed_fraction(ctx.site.tz, observed)
        per_unit = 0.0
        d = p.receive_date
        while d <= sale_day:
            dims = storage_dims(ctx, msku, ctx.dims.for_month(msku, month_str(d)))
            if dims is None:
                return None, "no_volume"
            age = (d - p.receive_date).days
            aged = pricing.aged_row(ctx.price, ctx.country, age) if age >= 181 else None
            unit = pricing.monthly_unit_fee(ctx.price, ctx.country, dims, d, aged)
            if unit is None:
                return None, "no_rate"
            day_share = frac if d == sale_day else 1.0
            per_unit += unit / days_in_month(d) * day_share
            d += timedelta(days=1)
        total += per_unit * p.qty
    return total, None


def compute_rows(ctx: StoreContext) -> list[dict]:
    rows = []
    mskus = set(ctx.profit)
    if ctx.age:
        mskus |= set(ctx.age.by_msku)
    rate = ctx.fx.rate if ctx.fx else None
    frac = ctx.elapsed
    for msku in sorted(mskus):
        p = ctx.profit.get(msku)
        if p is None:
            p = _zero_profit(msku, ctx.sid)
        else:
            p = {**p, "in_order_profit": True}
        dims = ctx.dims.current(msku)
        measured = storage_dims(ctx, msku, dims)
        afn = max(0.0, p["afn_volume"])

        # ---- 单件弃置费。仓储报告没有体积时，重量和边长用 Listing。
        disp_status = "ok"
        disp_local = None
        disp_note = ""
        disp_dims = dims if dims is not None and dims.volume_cm3() else measured
        if disp_dims is None:
            if not ctx.dims.months:
                disp_status = "no_report"
            elif dims is None:
                unmapped_now = ctx.dims.unmapped.get(ctx.dims.latest_month or "", set())
                disp_status = "unmapped" if (p["fnsku"] and p["fnsku"] in unmapped_now) else "no_dims"
            else:
                disp_status = "no_dims"
        else:
            disp_local, disp_note = pricing.disposal_fee_local(ctx.price, ctx.country, disp_dims)
            if disp_local is None:
                disp_status = "no_rate"
            elif rate is None:
                disp_status = "no_fx"
        disposal_cny = disp_local * rate if (disp_local is not None and rate is not None) else 0.0

        # ---- 预计退货成本。查不到 Q 时按 0，手填覆盖接口值。
        net, selling = p["net_amount"], p["selling_fee"]
        if msku in ctx.q.manual:
            q, q_source = ctx.q.q[msku], "manual"
        elif msku in ctx.q.q:
            q, q_source = ctx.q.q[msku], "api"
        else:
            q, q_source = 0.0, "assumed"
        disposal_part = disposal_cny * afn if afn > 0 else 0.0
        rc, rc_status = q * (net - 0.8 * abs(selling) + disposal_part), "ok"

        # ---- 单日仓储费（日模式）
        buckets = ctx.age.by_msku.get(msku) if ctx.age else None
        daily_cny = elapsed_cny = None
        daily_status = "ok"
        bucket_detail: list[dict] = []
        if buckets is None:
            daily_status = "no_age"
        else:
            local, err, bucket_detail = daily_storage_local(ctx, buckets, measured, ctx.local_date)
            if err:
                daily_status = err
            elif local and rate is None:
                daily_status = "no_fx"
            else:
                daily_cny = (local or 0.0) * (rate or 0.0)
                elapsed_cny = daily_cny * frac

        # ---- 订单仓储费（先入先出模式）
        pieces = ctx.sold.get(msku, [])
        known = sum(x.qty for x in pieces if x.kind == "received")
        unknown = sum(x.qty for x in pieces if x.kind != "received")
        order_cny = None
        if ctx.t0 is None and afn > 0:
            order_status = "no_t0"
        elif unknown > 0:
            order_status = "unknown_lot"
        elif not pieces:
            order_status, order_cny = "ok", 0.0
        else:
            local, err = order_storage_local(ctx, msku, pieces)
            if err:
                order_status = err
            elif rate is None:
                order_status = "no_fx"
            else:
                order_status, order_cny = "ok", local * rate

        # ---- 调整后毛利
        addbacks = {f: addback(p[f], ctx.cost_sign) for f in REPLACED_FIELDS}
        base = p["gross_profit"] + sum(addbacks.values())
        adj = {}
        for mode, cost, status in ((MODE_DAILY, elapsed_cny, daily_status), (MODE_ORDER, order_cny, order_status)):
            ready = rc_status == "ok" and status == "ok" and cost is not None
            adj[mode] = base - rc - cost if ready else None  # type: ignore[operator]

        rows.append({
            "msku": msku,
            "fnsku": p["fnsku"] or (disp_dims.fnsku if disp_dims else ""),
            "asin": p["asin"],
            "local_name": p["local_name"],
            "in_order_profit": p["in_order_profit"],
            "afn_volume": afn,
            "net_amount": net,
            "selling_fee": selling,
            "gross_profit": p["gross_profit"],
            "replaced": {f: p[f] for f in REPLACED_FIELDS},
            "addbacks": addbacks,
            "retained": {f: p.get(f) for f in RETAINED_FIELDS},
            "q": q,
            "q_source": q_source,
            "q_flagged": q_source == "api" and msku in ctx.q.flagged,
            "return_cost": rc,
            "return_cost_status": rc_status,
            "disposal_unit_local": disp_local,
            "disposal_unit_cny": disposal_cny,
            "disposal_status": disp_status,
            "disposal_note": disp_note,
            "size_tier": (dims.size_tier if dims and dims.size_tier else (disp_dims.size_tier if disp_dims else "")),
            "dims_month": dims.month if dims and dims.volume_cm3() else (disp_dims.month if disp_dims else None),
            "daily_storage_cny": daily_cny,
            "elapsed_storage_cny": elapsed_cny,
            "daily_storage_status": daily_status,
            "age_buckets": buckets,
            "bucket_detail": bucket_detail,
            "order_storage_cny": order_cny,
            "order_storage_status": order_status,
            "order_known_units": known,
            "order_unknown_units": unknown,
            "adjusted": adj,
            "missing_fields": p.get("missing_fields", []),
        })
    return rows


def _excluded_reason(row: dict, mode: str) -> str:
    if row["return_cost_status"] != "ok":
        return row["return_cost_status"]
    return row["daily_storage_status"] if mode == MODE_DAILY else row["order_storage_status"]


def summarize_mode(rows: list[dict], mode: str) -> dict:
    included = [r for r in rows if r["adjusted"][mode] is not None]
    excluded = [r for r in rows if r["adjusted"][mode] is None]
    storage_key = "elapsed_storage_cny" if mode == MODE_DAILY else "order_storage_cny"
    gp = sum(r["gross_profit"] for r in included)
    addbacks = {f: sum(r["addbacks"][f] for r in included) for f in REPLACED_FIELDS}
    rc = sum(r["return_cost"] for r in included)
    storage = sum(r[storage_key] for r in included)
    adjusted = sum(r["adjusted"][mode] for r in included)
    net = sum(r["net_amount"] for r in included)
    retained = {}
    for f, (label, _names) in RETAINED_FIELDS.items():
        vals = [r["retained"][f] for r in included if r["retained"][f] is not None]
        retained[f] = {"label": label, "amount": sum(vals) if vals else None}
    reasons: dict[str, int] = {}
    for r in excluded:
        k = _excluded_reason(r, mode)
        reasons[k] = reasons.get(k, 0) + 1
    steps = [{"key": "gross_profit", "label": "原毛利", "amount": gp}]
    steps += [{"key": f, "label": ADDBACK_LABELS[f], "amount": addbacks[f]} for f in REPLACED_FIELDS]
    steps += [
        {"key": "return_cost", "label": "减预计退货成本", "amount": -rc},
        {"key": "storage", "label": f"减{MODE_LABELS[mode]}", "amount": -storage},
    ]
    has_included = bool(included) or not rows
    return {
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "adjusted": adjusted if has_included else None,
        "margin": adjusted / net if (net and has_included) else None,
        "net_amount": net,
        "steps": steps,
        "steps_total": sum(s["amount"] for s in steps),
        "retained": retained,
        "included": len(included),
        "excluded": len(excluded),
        "excluded_reasons": reasons,
        "excluded_gross_profit": sum(r["gross_profit"] for r in excluded),
        "fallback_to_daily": False,
    }


def summarize(rows: list[dict]) -> dict:
    daily = summarize_mode(rows, MODE_DAILY)
    order = summarize_mode(rows, MODE_ORDER)
    selling_rows = [r for r in rows if r["afn_volume"] > 0 or r["order_known_units"] or r["order_unknown_units"]]
    order_ready = [r for r in selling_rows if r["order_storage_status"] == "ok"]
    order_unready = [r for r in selling_rows if r["order_storage_status"] != "ok"]
    if selling_rows and not order_ready:
        order = {**daily, "mode": MODE_ORDER, "mode_label": MODE_LABELS[MODE_ORDER], "fallback_to_daily": True}
    order["order_unready_mskus"] = len(order_unready)
    order["order_known_units"] = sum(r["order_known_units"] for r in rows)
    order["order_unknown_units"] = sum(r["order_unknown_units"] for r in rows)
    return {
        MODE_DAILY: daily,
        MODE_ORDER: order,
        "msku_count": len(rows),
        "no_q_mskus": sum(1 for r in rows if r["q_source"] == "assumed" and (r["afn_volume"] or r["net_amount"])),
        "disposal_issue_counts": _count(rows, "disposal_status"),
        "daily_storage_issue_counts": _count(rows, "daily_storage_status"),
        "order_storage_issue_counts": _count(rows, "order_storage_status"),
        "afn_volume": sum(r["afn_volume"] for r in rows),
    }


def _count(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        if r[key] != "ok":
            out[r[key]] = out.get(r[key], 0) + 1
    return out


# --------------------------------------------------------------------------- 符号验收
def sign_check(sid: int, rows: list[dict], cost_sign: str) -> dict | None:
    """选一个原退款/原仓储不为 0 的 msku 手算。加回后利润应按费用量级上升，否则视为符号不对。

    返回 None 表示本轮没有可用样本。
    """
    candidates = [r for r in rows if r["in_order_profit"]
                  and sum(abs(r["replaced"][f]) for f in PRIMARY_SIGN_FIELDS) > 0]
    if not candidates:
        return None
    r = max(candidates, key=lambda x: sum(abs(x["replaced"][f]) for f in PRIMARY_SIGN_FIELDS))
    primary = {f: r["replaced"][f] for f in PRIMARY_SIGN_FIELDS}
    change = sum(addback(v, cost_sign) for v in primary.values())
    magnitude = sum(abs(v) for v in primary.values())
    ok = abs(change - magnitude) <= max(0.01, magnitude * 1e-6)
    gp = r["gross_profit"]
    all_addbacks = {f: addback(r["replaced"][f], cost_sign) for f in REPLACED_FIELDS}
    expected_sign = "≤ 0" if cost_sign == "negative" else "≥ 0"
    field_signs = {
        f: {"value": v, "expected": expected_sign,
            "ok": (v <= 0) if cost_sign == "negative" else (v >= 0)}
        for f, v in r["replaced"].items()
    }
    lines = [f"gross_profit = {gp:.2f}"]
    for f, v in r["replaced"].items():
        lines.append(f"{'−' if cost_sign == 'negative' else '+'} {f} ({v:.2f}) → {all_addbacks[f]:+.2f}")
    base = gp + sum(all_addbacks.values())
    lines.append(f"= 剔除四类原值后 {base:.2f}（变化 {base - gp:+.2f}，主要费用量级 {magnitude:.2f}）")
    if r["return_cost"] is not None:
        lines.append(f"− 预计退货成本 {r['return_cost']:.2f}")
    if r["elapsed_storage_cny"] is not None:
        lines.append(f"− 已过时段在库仓储 {r['elapsed_storage_cny']:.2f}")
    if r["adjusted"][MODE_DAILY] is not None:
        lines.append(f"= 调整后毛利 {r['adjusted'][MODE_DAILY]:.2f}")
    return {
        "status": "passed" if ok else "failed",
        "cost_sign": cost_sign,
        "sid": sid,
        "msku": r["msku"],
        "gross_profit": gp,
        "change": change,
        "magnitude": magnitude,
        "field_signs": field_signs,
        "lines": lines,
    }
