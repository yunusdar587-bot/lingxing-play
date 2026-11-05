"""OrderProfit 行字段归一。主键 sid + msku；同主键多行时数值相加。"""
from __future__ import annotations

from typing import Any

# 被替换的四类原值（参与代数剔除）
REPLACED_FIELDS = (
    "refund_amount",
    "fba_storage_fee",
    "shared_fba_storage_fee",
    "long_term_storage_fee",
    "shared_long_term_storage_fee",
    "shared_fba_disposal_fee",
)

# 仍留在毛利里、仅供展示的费用。领星不同版本字段名略有差异，按顺序取第一个存在的。
RETAINED_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "platform_fee": ("平台费", ("selling_fee", "sellingFee", "commission_fee")),
    "fba_fee": ("FBA 发货费", ("fba_delivery_fee", "fba_fee", "fba_shipping_fee", "fbaDeliveryFee")),
    "ads_fee": ("广告费", ("total_ads_cost", "ads_cost", "ad_cost", "totalAdsCost")),
    "purchase_cost": ("采购成本", ("cg_price_total", "purchase_cost", "cg_price", "cgPriceTotal")),
    "head_freight": ("头程", ("cg_transport_costs_total", "cg_transport_costs", "first_leg_fee", "head_freight")),
}
ADS_COMPONENTS = ("ads_sp_cost", "ads_sb_cost", "ads_sbv_cost", "ads_sd_cost")

NUMERIC_FIELDS = ("gross_profit", "net_amount", "selling_fee", "afn_volume", *REPLACED_FIELDS)

ALIASES: dict[str, tuple[str, ...]] = {
    "gross_profit": ("gross_profit", "grossProfit"),
    "net_amount": ("net_amount", "netAmount"),
    "selling_fee": ("selling_fee", "sellingFee"),
    "afn_volume": ("afn_volume", "afnVolume"),
    "refund_amount": ("refund_amount", "refundAmount"),
    "fba_storage_fee": ("fba_storage_fee", "fbaStorageFee"),
    "shared_fba_storage_fee": ("shared_fba_storage_fee", "sharedFbaStorageFee"),
    "long_term_storage_fee": ("long_term_storage_fee", "longTermStorageFee"),
    "shared_long_term_storage_fee": ("shared_long_term_storage_fee", "sharedLongTermStorageFee"),
    "shared_fba_disposal_fee": ("shared_fba_disposal_fee", "sharedFbaDisposalFee"),
    "return_rate": ("return_rate", "returnRate"),
    "msku": ("msku", "seller_sku", "sellerSku"),
    "fnsku": ("fnsku",),
    "sid": ("sid",),
    "asin": ("asin",),
    "local_name": ("local_name", "product_name", "item_name"),
}


def num(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if s.endswith("%"):
        s = s[:-1]
    try:
        return float(s)
    except ValueError:
        return 0.0


def pick(row: dict, names: tuple[str, ...]) -> Any:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def has_any(row: dict, names: tuple[str, ...]) -> bool:
    return any(n in row for n in names)


def _first_dict(value: Any) -> dict:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def order_profit_view(raw: dict) -> dict:
    """MSKU 维度的 seller_sku、sid 在 price_list / sids 里，不在最外层。"""
    row = dict(raw)
    item = _first_dict(raw.get("price_list"))
    for key in ("seller_sku", "sid", "asin", "fnsku", "local_name", "item_name"):
        if row.get(key) in (None, "") and item.get(key) not in (None, ""):
            row[key] = item[key]
    if row.get("sid") in (None, ""):
        sids = raw.get("sids")
        if isinstance(sids, list) and sids:
            row["sid"] = sids[0]
        elif sids not in (None, ""):
            row["sid"] = sids
    if row.get("asin") in (None, ""):
        asin = _first_dict(raw.get("asins")).get("asin")
        if asin not in (None, ""):
            row["asin"] = asin
    local_name = _first_dict(raw.get("local_infos")).get("local_name")
    if row.get("local_name") in (None, "") and local_name not in (None, ""):
        row["local_name"] = local_name
    return row


def normalize_row(raw: dict, default_sid: int | None = None) -> dict | None:
    raw = order_profit_view(raw)
    msku = pick(raw, ALIASES["msku"])
    if msku in (None, ""):
        return None
    sid = pick(raw, ALIASES["sid"])
    sid = int(sid) if sid not in (None, "") else default_sid
    if sid is None:
        return None
    out: dict[str, Any] = {
        "sid": sid,
        "msku": str(msku),
        "fnsku": str(pick(raw, ALIASES["fnsku"]) or ""),
        "asin": str(pick(raw, ALIASES["asin"]) or ""),
        "local_name": str(pick(raw, ALIASES["local_name"]) or ""),
        "return_rate_raw": pick(raw, ALIASES["return_rate"]),
        "missing_fields": [f for f in NUMERIC_FIELDS if not has_any(raw, ALIASES[f])],
    }
    for f in NUMERIC_FIELDS:
        out[f] = num(pick(raw, ALIASES[f]))
    for key, (_label, names) in RETAINED_FIELDS.items():
        v = pick(raw, names)
        if v is None and key == "ads_fee":
            out[key] = sum(num(raw.get(n)) for n in ADS_COMPONENTS) if has_any(raw, ADS_COMPONENTS) else None
        else:
            out[key] = num(v) if v is not None else None
    return out


def aggregate(rows: list[dict]) -> dict[tuple[int, str], dict]:
    """同一 sid + msku 的多行数值相加（OrderProfit 无订单号，不拆订单）。"""
    out: dict[tuple[int, str], dict] = {}
    for r in rows:
        key = (r["sid"], r["msku"])
        if key not in out:
            out[key] = dict(r)
            out[key]["source_rows"] = 1
            continue
        agg = out[key]
        agg["source_rows"] += 1
        for f in NUMERIC_FIELDS:
            agg[f] += r[f]
        for f in RETAINED_FIELDS:
            if r.get(f) is not None:
                agg[f] = (agg.get(f) or 0.0) + r[f]
        if not agg["fnsku"] and r["fnsku"]:
            agg["fnsku"] = r["fnsku"]
        agg["missing_fields"] = sorted(set(agg["missing_fields"]) & set(r["missing_fields"]))
    return out


def parse_return_rate(raw: Any) -> tuple[float | None, bool]:
    """返回 (Q, 是否打标)。带 % 除以 100；不带 % 且 > 1 也除以 100 并打标；其余视为小数。"""
    if raw in (None, ""):
        return None, False
    if isinstance(raw, (int, float)):
        v = float(raw)
        return (v / 100.0, True) if v > 1 else (v, False)
    s = str(raw).strip().replace(",", "")
    if not s:
        return None, False
    if s.endswith("%"):
        try:
            return float(s[:-1].strip()) / 100.0, False
        except ValueError:
            return None, False
    try:
        v = float(s)
    except ValueError:
        return None, False
    if v > 1:
        return v / 100.0, True
    return v, False
