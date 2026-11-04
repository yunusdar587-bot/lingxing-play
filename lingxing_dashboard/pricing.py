"""亚马逊费用价目表（写在代码里，带版本号与生效日期）及计算。

只覆盖：弃置费（单件）、月仓储费、181 天起的库龄附加费。全部商品按非危险品、非服装处理。
新版本加进 PRICE_TABLES 后不会自动生效，需要在看板上人工确认。

数据来源见每段的 source；verified=False 的行未能从亚马逊官方页面逐项核对，看板会提示。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

STANDARD = ("small_standard", "large_standard")
OVERSIZE = ("small_oversize", "medium_oversize", "large_oversize", "special_oversize", "oversize")
ALL_MONTHS = tuple(range(1, 13))
OFF_PEAK = tuple(range(1, 10))
PEAK = (10, 11, 12)

# 日模式库龄桶（互不重叠）。181 天以上的桶按「桶下限所在档」取附加费；365+ 取 365 天以上第一档。
AGE_BUCKETS = (
    ("0-30", 0, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("91-180", 91, 180),
    ("181-270", 181, 270),
    ("271-365", 271, 365),
    ("365+", 366, None),
)


# --------------------------------------------------------------------------- 数据
def _us(disposal_small_std_first: float, aged_tail: list[dict]) -> dict:
    return {
        "currency": "USD",
        "size_rule": {
            "type": "tier",
            "length_unit": "in",
            "weight_unit": "lb",
            # 尺寸档文本缺失时的兜底判断（最长边、次长边、最短边、重量上限）
            "fallback": [
                ("small_standard", (15, 12, 0.75), 1.0),
                ("large_standard", (18, 14, 8), 20.0),
            ],
            "else": "oversize",
        },
        "storage": {
            "volume_unit": "ft3",
            "source": "Amazon.com Monthly inventory storage fees（非危险品）",
            "verified": True,
            "rows": [
                {"classes": STANDARD, "months": OFF_PEAK, "rate": 0.78},
                {"classes": STANDARD, "months": PEAK, "rate": 2.40},
                {"classes": OVERSIZE, "months": OFF_PEAK, "rate": 0.56},
                {"classes": OVERSIZE, "months": PEAK, "rate": 1.40},
            ],
        },
        "aged": {
            "volume_unit": "ft3",
            "source": "Amazon.com Aged inventory surcharge (JQNPA23YWVA4SBD)",
            "verified": True,
            "rows": [
                {"min_days": 181, "max_days": 210, "rate": 0.50},
                {"min_days": 211, "max_days": 240, "rate": 1.00},
                {"min_days": 241, "max_days": 270, "rate": 1.50},
                {"min_days": 271, "max_days": 300, "rate": 5.45},
                {"min_days": 301, "max_days": 330, "rate": 5.70},
                {"min_days": 331, "max_days": 365, "rate": 5.90},
                *aged_tail,
            ],
        },
        "disposal": {
            "weight_unit": "lb",
            "length_unit": "in",
            "source": "Amazon.com FBA removal and disposal order fees",
            "verified": True,
            # 小号标准件按单件实重；大号标准件及以上按实重与体积重取大，体积重除数 139 in³/lb。
            "rows": [
                {"classes": ("small_standard",), "min_w": 0, "max_w": 0.5, "fee": disposal_small_std_first,
                 "billing_weight": "actual"},
                {"classes": ("small_standard",), "min_w": 0.5, "max_w": None, "fee": 1.53,
                 "billing_weight": "actual"},
                {"classes": ("large_standard",), "min_w": 0, "max_w": 0.5, "fee": disposal_small_std_first,
                 "billing_weight": "max", "dim_divisor": 139},
                {"classes": ("large_standard",), "min_w": 0.5, "max_w": 1, "fee": 1.53,
                 "billing_weight": "max", "dim_divisor": 139},
                {"classes": ("large_standard",), "min_w": 1, "max_w": 2, "fee": 2.27,
                 "billing_weight": "max", "dim_divisor": 139},
                {"classes": ("large_standard",), "min_w": 2, "max_w": None, "fee": 2.89, "per_over": 1.06,
                 "over_base": 2, "round_step": 1, "billing_weight": "max", "dim_divisor": 139},
                {"classes": OVERSIZE, "min_w": 0, "max_w": 1, "fee": 3.12, "billing_weight": "max", "dim_divisor": 139},
                {"classes": OVERSIZE, "min_w": 1, "max_w": 2, "fee": 4.30, "billing_weight": "max", "dim_divisor": 139},
                {"classes": OVERSIZE, "min_w": 2, "max_w": 4, "fee": 6.36, "billing_weight": "max", "dim_divisor": 139},
                {"classes": OVERSIZE, "min_w": 4, "max_w": 10, "fee": 10.04, "billing_weight": "max", "dim_divisor": 139},
                {"classes": OVERSIZE, "min_w": 10, "max_w": None, "fee": 14.32, "per_over": 1.06, "over_base": 10,
                 "round_step": 1, "billing_weight": "max", "dim_divisor": 139},
            ],
        },
    }


def _ca() -> dict:
    return {
        "currency": "CAD",
        "size_rule": {
            "type": "tier",
            "length_unit": "cm",
            "weight_unit": "kg",
            "fallback": [
                ("small_standard", (38, 27, 2), 0.5),
                ("large_standard", (45, 35, 20), 9.0),
            ],
            "else": "oversize",
        },
        "storage": {
            "volume_unit": "m3",
            "source": "sell.amazon.ca/pricing（标准件含信封件）",
            "verified": True,
            "rows": [
                {"classes": STANDARD, "months": OFF_PEAK, "rate": 40.0},
                {"classes": STANDARD, "months": PEAK, "rate": 77.0},
                {"classes": OVERSIZE, "months": OFF_PEAK, "rate": 28.0},
                {"classes": OVERSIZE, "months": PEAK, "rate": 49.0},
            ],
        },
        "aged": {
            "volume_unit": "m3",
            "source": "第三方整理的 Amazon.ca 库龄附加费；301-330 天为插值",
            "verified": False,
            "rows": [
                {"min_days": 181, "max_days": 210, "rate": 24.0},
                {"min_days": 211, "max_days": 240, "rate": 48.0},
                {"min_days": 241, "max_days": 270, "rate": 72.0},
                {"min_days": 271, "max_days": 300, "rate": 156.0},
                {"min_days": 301, "max_days": 330, "rate": 172.0},
                {"min_days": 331, "max_days": 365, "rate": 189.0},
                {"min_days": 366, "max_days": None, "rate": 330.0, "min_per_unit": 0.15},
            ],
        },
        "disposal": {
            "weight_unit": "kg",
            "length_unit": "cm",
            "source": "第三方整理的 Amazon.ca 弃置费区间（CAD 0.30–3.53 + 0.59/kg 超 5kg）",
            "verified": False,
            "rows": [
                {"classes": ("small_standard",), "min_w": 0, "max_w": None, "fee": 0.30, "billing_weight": "actual"},
                {"classes": ("large_standard",), "min_w": 0, "max_w": None, "fee": 0.65,
                 "billing_weight": "max", "dim_divisor": 5000},
                {"classes": ("small_oversize",), "min_w": 0, "max_w": None, "fee": 1.62,
                 "billing_weight": "max", "dim_divisor": 5000},
                {"classes": ("medium_oversize",), "min_w": 0, "max_w": None, "fee": 2.20,
                 "billing_weight": "max", "dim_divisor": 5000},
                {"classes": ("large_oversize", "special_oversize", "oversize"), "min_w": 0, "max_w": 5, "fee": 3.53,
                 "billing_weight": "max", "dim_divisor": 5000},
                {"classes": ("large_oversize", "special_oversize", "oversize"), "min_w": 5, "max_w": None,
                 "fee": 3.53, "per_over": 0.59, "over_base": 5, "round_step": 1,
                 "billing_weight": "max", "dim_divisor": 5000},
            ],
        },
    }


def _mx() -> dict:
    return {
        "currency": "MXN",
        "size_rule": {
            "type": "tier",
            "length_unit": "cm",
            "weight_unit": "kg",
            "fallback": [("large_standard", (45, 35, 20), None)],
            "else": "oversize",
        },
        "storage": {
            "volume_unit": "dm3",
            "source": "vender.amazon.com.mx/logistica-de-amazon（含 IVA，2025-05-15 更新）",
            "verified": True,
            "rows": [
                {"classes": STANDARD, "months": OFF_PEAK, "rate": 0.36},
                {"classes": STANDARD, "months": PEAK, "rate": 0.53},
                {"classes": OVERSIZE, "months": OFF_PEAK, "rate": 0.19},
                {"classes": OVERSIZE, "months": PEAK, "rate": 0.43},
            ],
        },
        "aged": {
            "volume_unit": "dm3",
            "source": "vender.amazon.com.mx Recargo por almacenamiento prolongado（含 IVA）",
            "verified": True,
            "rows": [
                {"min_days": 181, "max_days": 210, "rate": 0.30},
                {"min_days": 211, "max_days": 240, "rate": 0.60},
                {"min_days": 241, "max_days": 270, "rate": 0.90},
                {"min_days": 271, "max_days": 300, "rate": 2.56},
                {"min_days": 301, "max_days": 330, "rate": 2.73},
                {"min_days": 331, "max_days": 365, "rate": 2.82},
                {"min_days": 366, "max_days": None, "rate": 4.76, "min_per_unit": 2.32},
            ],
        },
        "disposal": {
            "weight_unit": "kg",
            "length_unit": "cm",
            "source": "vender.amazon.com.mx：destrucción 平均 MXN 13.00/件（含 IVA，官方未公布分档）",
            "verified": False,
            "rows": [{"classes": None, "min_w": 0, "max_w": None, "fee": 13.00, "billing_weight": "actual"}],
        },
    }


def _br() -> dict:
    return {
        "currency": "BRL",
        # 巴西月仓储费按单件体积是否满 10,000 cm³ 区分，不看尺寸档。
        "size_rule": {"type": "none"},
        "storage": {
            "volume_unit": "m3",
            "source": "venda.amazon.com.br/precos（2025-08-01 更新，无淡旺季）",
            "verified": True,
            "rows": [
                {"classes": None, "months": ALL_MONTHS, "unit_volume_cm3_lt": 10000, "rate": 75.0},
                {"classes": None, "months": ALL_MONTHS, "unit_volume_cm3_gte": 10000, "rate": 37.5},
            ],
        },
        "aged": {
            "volume_unit": "m3",
            "source": "venda.amazon.com.br：armazenagem de longo prazo，超过 365 天",
            "verified": True,
            "rows": [{"min_days": 366, "max_days": None, "rate": 525.0}],
        },
        "disposal": {
            "weight_unit": "kg",
            "length_unit": "cm",
            "source": "venda.amazon.com.br/cresca/fba Tarifa de remoção（按重量与尺寸，体积重 cm³/6000）",
            "verified": True,
            "rows": [
                *[
                    {"classes": None, "min_w": lo, "max_w": hi, "fee": fee, "billing_weight": "max", "dim_divisor": 6000}
                    for lo, hi, fee in (
                        (0, 0.25, 0.99), (0.25, 0.5, 1.04), (0.5, 1, 1.12), (1, 2, 1.26), (2, 3, 1.76),
                        (3, 4, 1.90), (4, 5, 2.00), (5, 6, 2.20), (6, 7, 2.40), (7, 8, 2.60), (8, 9, 3.00),
                        (9, 10, 3.60),
                    )
                ],
                {"classes": None, "min_w": 10, "max_w": None, "fee": 3.60, "per_over": 0.17, "over_base": 10,
                 "round_step": 1, "billing_weight": "max", "dim_divisor": 6000},
            ],
        },
    }


PRICE_TABLES: list[dict] = [
    {
        "version": "2025.1",
        "effective_date": "2025-01-15",
        "bootstrap": True,
        "notes": "美国 2025 费率：小号标准件弃置首档 $1.04；365 天以上附加费 $6.90/ft³ 或 $0.15/件取大。",
        "countries": {
            "US": _us(1.04, [{"min_days": 366, "max_days": None, "rate": 6.90, "min_per_unit": 0.15}]),
            "CA": _ca(),
            "MX": _mx(),
            "BR": _br(),
        },
    },
    {
        "version": "2026.1",
        "effective_date": "2026-01-16",
        "notes": "美国 2026-01-15 起标准件 0–0.5 lb 弃置费降为 $0.84；2026-01-16 起 366–455 天 $6.90/ft³ 或 $0.30/件，"
                 "456 天以上 $7.90/ft³ 或 $0.35/件。其他三国沿用上一版。",
        "countries": {
            "US": _us(0.84, [
                {"min_days": 366, "max_days": 455, "rate": 6.90, "min_per_unit": 0.30},
                {"min_days": 456, "max_days": None, "rate": 7.90, "min_per_unit": 0.35},
            ]),
            "CA": _ca(),
            "MX": _mx(),
            "BR": _br(),
        },
    },
]


def versions() -> list[dict]:
    return sorted(PRICE_TABLES, key=lambda t: (t["effective_date"], t["version"]))


def get_version(version: str) -> dict | None:
    for t in PRICE_TABLES:
        if t["version"] == version:
            return copy.deepcopy(t)
    return None


def bootstrap_version() -> str:
    boot = [t for t in versions() if t.get("bootstrap")]
    return (boot or versions())[0]["version"]


def unverified_sections(table: dict) -> list[dict]:
    out = []
    for country, spec in table["countries"].items():
        for section in ("storage", "aged", "disposal"):
            if not spec[section].get("verified", False):
                out.append({"country": country, "section": section, "source": spec[section].get("source", "")})
    return out


# --------------------------------------------------------------------------- 单位
_LENGTH = {"in": 2.54, "inch": 2.54, "inches": 2.54, "cm": 1.0, "centimeter": 1.0, "centimeters": 1.0,
           "centimetre": 1.0, "centimetres": 1.0, "mm": 0.1, "m": 100.0}
_WEIGHT = {"lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237, "kg": 1.0,
           "kilogram": 1.0, "kilograms": 1.0, "g": 0.001, "gram": 0.001, "grams": 0.001, "oz": 0.028349523125,
           "ounce": 0.028349523125, "ounces": 0.028349523125}
# 体积统一换算到 cm³
_VOLUME = {"ft3": 28316.846592, "cubic feet": 28316.846592, "cubic foot": 28316.846592, "cu ft": 28316.846592,
           "m3": 1e6, "cubic meters": 1e6, "cubic metres": 1e6, "cubic meter": 1e6, "cubic metre": 1e6,
           "dm3": 1000.0, "liters": 1000.0, "litres": 1000.0, "liter": 1000.0, "l": 1000.0,
           "cm3": 1.0, "cubic centimeters": 1.0, "cubic centimetres": 1.0, "cubic centimeter": 1.0,
           "in3": 16.387064, "cubic inches": 16.387064, "cubic inch": 16.387064}


def _unit_key(u: str | None) -> str:
    return str(u or "").strip().lower().replace("_", " ")


def length_factor(unit: str | None) -> float | None:
    return _LENGTH.get(_unit_key(unit))


def weight_factor(unit: str | None) -> float | None:
    return _WEIGHT.get(_unit_key(unit))


def volume_factor(unit: str | None) -> float | None:
    return _VOLUME.get(_unit_key(unit))


@dataclass
class Dims:
    fnsku: str = ""
    month: str = ""
    longest: float = 0.0
    median: float = 0.0
    shortest: float = 0.0
    length_unit: str = ""
    weight: float = 0.0
    weight_unit: str = ""
    volume: float = 0.0
    volume_unit: str = ""
    size_tier: str = ""
    country_code: str = ""
    aqoh: float = 0.0
    fulfillment_center: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def sides_cm(self) -> tuple[float, float, float] | None:
        f = length_factor(self.length_unit)
        if f is None or min(self.longest, self.median, self.shortest) <= 0:
            return None
        return tuple(sorted((self.longest * f, self.median * f, self.shortest * f), reverse=True))  # type: ignore

    def weight_kg(self) -> float | None:
        f = weight_factor(self.weight_unit)
        if f is None or self.weight <= 0:
            return None
        return self.weight * f

    def volume_cm3(self) -> float | None:
        f = volume_factor(self.volume_unit)
        if f is not None and self.volume > 0:
            return self.volume * f
        sides = self.sides_cm()
        if sides:
            return sides[0] * sides[1] * sides[2]
        return None


# --------------------------------------------------------------------------- 计算
def tier_class(text: str) -> str | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    if "small standard" in t or "envelope" in t or "sobre" in t:
        return "small_standard"
    if "large standard" in t:
        return "large_standard"
    if "small oversize" in t:
        return "small_oversize"
    if "medium oversize" in t:
        return "medium_oversize"
    if "large oversize" in t:
        return "large_oversize"
    if "special oversize" in t:
        return "special_oversize"
    if any(k in t for k in ("bulky", "extra-large", "extra large", "oversize", "grande", "volumoso")):
        return "oversize"
    if any(k in t for k in ("standard", "estándar", "estandar", "padrão", "padrao")):
        return "standard"
    return None


def size_class(spec: dict, dims: Dims) -> str | None:
    rule = spec["size_rule"]
    if rule["type"] == "none":
        return "any"
    cls = tier_class(dims.size_tier)
    if cls and cls != "standard":
        return cls
    sides = dims.sides_cm()
    wkg = dims.weight_kg()
    if sides is None:
        return "large_standard" if cls == "standard" else None
    lf = length_factor(rule["length_unit"])
    wf = weight_factor(rule["weight_unit"])
    local_sides = [s / lf for s in sides]
    local_w = wkg / wf if wkg is not None else None
    for name, limits, wmax in rule["fallback"]:
        fits = all(s <= lim for s, lim in zip(local_sides, limits))
        if fits and (wmax is None or (local_w is not None and local_w <= wmax)):
            if cls == "standard" and name not in STANDARD:
                continue
            return name
    if cls == "standard":
        return "large_standard"
    return rule["else"]


def _class_ok(row: dict, cls: str) -> bool:
    return row.get("classes") is None or cls in row["classes"]


def _billing_weight(row: dict, dims: Dims, weight_unit: str, length_unit: str) -> float | None:
    wkg = dims.weight_kg()
    actual = wkg / weight_factor(weight_unit) if wkg is not None else None
    dim_w = None
    if row.get("dim_divisor"):
        sides = dims.sides_cm()
        if sides:
            lf = length_factor(length_unit)
            l, m, s = (x / lf for x in sides)
            dim_w = l * m * s / row["dim_divisor"]
    mode = row.get("billing_weight", "actual")
    if mode == "actual":
        return actual
    if mode == "dimensional":
        return dim_w
    vals = [v for v in (actual, dim_w) if v is not None]
    return max(vals) if vals else None


def disposal_fee_local(table: dict, country: str, dims: Dims | None) -> tuple[float | None, str]:
    """单件弃置费（站点本币）。返回 (金额, 说明)；无法计算时金额为 None。"""
    if dims is None:
        return None, "无尺寸"
    spec = table["countries"].get(country)
    if not spec:
        return None, f"价目表未覆盖 {country}"
    cls = size_class(spec, dims)
    if cls is None:
        return None, "无法判断尺寸档"
    disp = spec["disposal"]
    for row in disp["rows"]:
        if not _class_ok(row, cls):
            continue
        w = _billing_weight(row, dims, disp["weight_unit"], disp["length_unit"])
        if w is None:
            return None, "缺少重量或边长"
        lo, hi = row["min_w"], row["max_w"]
        if (w > lo or (lo == 0 and w >= 0)) and (hi is None or w <= hi):
            fee = row["fee"]
            if row.get("per_over"):
                step = row.get("round_step", 1)
                over = max(0.0, w - row["over_base"])
                fee += row["per_over"] * math.ceil(round(over / step, 9)) * step
            return round(fee, 4), f"{cls} 计费重 {w:.3f} {disp['weight_unit']}（{row.get('billing_weight')}）"
    return None, f"{cls} 无匹配价目行"


def base_storage_rate(table: dict, country: str, dims: Dims, d: date) -> tuple[float, str] | None:
    """返回 (每体积单位月费率, 体积单位)。"""
    spec = table["countries"].get(country)
    if not spec:
        return None
    cls = size_class(spec, dims)
    if cls is None:
        return None
    vol = dims.volume_cm3()
    st = spec["storage"]
    for row in st["rows"]:
        if not _class_ok(row, cls) or d.month not in row["months"]:
            continue
        if "unit_volume_cm3_lt" in row and (vol is None or vol >= row["unit_volume_cm3_lt"]):
            continue
        if "unit_volume_cm3_gte" in row and (vol is None or vol < row["unit_volume_cm3_gte"]):
            continue
        return row["rate"], st["volume_unit"]
    return None


def aged_row(table: dict, country: str, age_days: int) -> dict | None:
    spec = table["countries"].get(country)
    if not spec:
        return None
    for row in spec["aged"]["rows"]:
        if age_days >= row["min_days"] and (row["max_days"] is None or age_days <= row["max_days"]):
            return row
    return None


def aged_row_for_bucket(table: dict, country: str, bucket_min: int) -> dict | None:
    """日模式：181-270 / 271-365 取桶下限所在档；365+ 取 365 天以上的第一档。"""
    spec = table["countries"].get(country)
    if not spec:
        return None
    if bucket_min > 365:
        tail = [r for r in spec["aged"]["rows"] if r["min_days"] > 365]
        return min(tail, key=lambda r: r["min_days"]) if tail else None
    return aged_row(table, country, bucket_min)


def monthly_unit_fee(table: dict, country: str, dims: Dims, d: date, aged: dict | None) -> float | None:
    """单件当月费用（本币）= 基础月仓储 + 库龄附加费（有最低每件金额时取大）。"""
    spec = table["countries"].get(country)
    vol = dims.volume_cm3()
    base = base_storage_rate(table, country, dims, d)
    if spec is None or vol is None or vol <= 0 or base is None:
        return None
    rate, unit = base
    unit_vol = vol / volume_factor(unit)
    fee = rate * unit_vol
    if aged:
        aunit = spec["aged"]["volume_unit"]
        surcharge = aged["rate"] * vol / volume_factor(aunit)
        fee += max(surcharge, aged.get("min_per_unit", 0.0))
    return fee
