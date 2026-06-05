"""Pure timezone-resolution helpers shared by writers, backfills, and the agent.

No I/O - all functions take primitives or PB-row dicts in and return values.
Caller is responsible for DB reads/writes.

Resolution order (see docs/superpowers/specs/2026-06-05-timezone-design.md §4):
    1. stop.timezone
    2. timezonefinder(lat, lng)
    3. day.timezone
    4. phone_tz
    5. None
"""
from __future__ import annotations

from datetime import date as _date, time as _time, datetime as _dt, timezone as _tz
from typing import Optional
from zoneinfo import ZoneInfo

try:
    from timezonefinder import TimezoneFinder
    _tf = TimezoneFinder()
except Exception:  # pragma: no cover - only hit if dep missing
    _tf = None


def gps_to_tz(*, lat: float, lng: float) -> Optional[str]:
    """Return IANA tz name for (lat, lng) or None if not resolvable."""
    if _tf is None:
        return None
    return _tf.timezone_at(lng=lng, lat=lat)


def resolve_tz(
    *,
    stop: Optional[dict] = None,
    day: Optional[dict] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    phone_tz: Optional[str] = None,
) -> Optional[str]:
    """Apply the fallback chain. Each input may be omitted; missing -> skip."""
    if stop:
        s_tz = (stop.get("timezone") or "").strip()
        if s_tz:
            return s_tz
    if lat is not None and lng is not None:
        got = gps_to_tz(lat=lat, lng=lng)
        if got:
            return got
    if day:
        d_tz = (day.get("timezone") or "").strip()
        if d_tz:
            return d_tz
    if phone_tz:
        return phone_tz
    return None


def compute_due_at(local_date: _date, local_time: _time, tz_name: str) -> _dt:
    """Compose (date, time, IANA tz) into a UTC-aware datetime.

    DST handled by zoneinfo automatically. Raises ZoneInfoNotFoundError
    on bad tz_name - caller decides whether to fall back.
    """
    local_dt = _dt.combine(local_date, local_time).replace(tzinfo=ZoneInfo(tz_name))
    return local_dt.astimezone(_tz.utc)
