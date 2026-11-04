from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Site:
    code: str
    name: str
    marketplace_id: str
    tz_name: str
    currency: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)


SITES: dict[str, Site] = {
    "US": Site("US", "美国", "ATVPDKIKX0DER", "America/Los_Angeles", "USD"),
    "CA": Site("CA", "加拿大", "A2EUQ1WTGCTBG2", "America/Toronto", "CAD"),
    "MX": Site("MX", "墨西哥", "A1AM78C64UM0Y8", "America/Mexico_City", "MXN"),
    "BR": Site("BR", "巴西", "A2Q3Y263D00KWC", "America/Sao_Paulo", "BRL"),
}

_BY_MARKETPLACE = {s.marketplace_id: s for s in SITES.values()}
_BY_NAME = {s.name: s for s in SITES.values()}


def site_for_seller(row: dict) -> Site | None:
    """按领星店铺接口返回的 marketplace_id / country 判断站点；其余站点返回 None。"""
    mp = str(row.get("marketplace_id") or "").strip()
    if mp in _BY_MARKETPLACE:
        return _BY_MARKETPLACE[mp]
    country = str(row.get("country") or "").strip()
    if country in _BY_NAME:
        return _BY_NAME[country]
    if country.upper() in SITES:
        return SITES[country.upper()]
    return None
