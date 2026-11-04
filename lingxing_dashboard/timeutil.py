from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_now(tz: ZoneInfo, now: datetime | None = None) -> datetime:
    return (now or utcnow()).astimezone(tz)


def local_midnight(tz: ZoneInfo, d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=tz)


def elapsed_seconds(tz: ZoneInfo, now: datetime | None = None) -> float:
    ln = local_now(tz, now)
    return (ln - local_midnight(tz, ln.date())).total_seconds()


def elapsed_fraction(tz: ZoneInfo, now: datetime | None = None) -> float:
    """站点本地已过秒数 / 86400，夏令时切换日封顶为 1。"""
    return max(0.0, min(1.0, elapsed_seconds(tz, now) / 86400.0))


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def prev_month(d: date) -> date:
    first = d.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_local_datetime(value, tz: ZoneInfo) -> datetime | None:
    """解析站点本地时间字符串；只有日期时返回 None，由调用方按日期比较。"""
    if value in (None, ""):
        return None
    s = str(value).strip()
    if len(s) <= 10:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)
