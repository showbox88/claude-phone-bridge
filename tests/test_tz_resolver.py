"""Unit tests for tz_resolver - pure functions, no I/O."""
from datetime import date, time, datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tz_resolver import resolve_tz, compute_due_at, gps_to_tz


def test_resolve_uses_stop_tz_when_present():
    assert resolve_tz(stop={"timezone": "Asia/Tokyo"}) == "Asia/Tokyo"


def test_resolve_falls_back_to_gps_when_stop_has_no_tz():
    # Tokyo Station coords
    assert resolve_tz(stop={"timezone": ""}, lat=35.6812, lng=139.7671) == "Asia/Tokyo"


def test_resolve_falls_back_to_day_tz_when_no_stop_no_gps():
    assert resolve_tz(day={"timezone": "Europe/Paris"}) == "Europe/Paris"


def test_resolve_falls_back_to_phone_tz_last():
    assert resolve_tz(phone_tz="America/Los_Angeles") == "America/Los_Angeles"


def test_resolve_returns_none_when_nothing_known():
    assert resolve_tz() is None


def test_gps_to_tz_paris():
    assert gps_to_tz(lat=48.8566, lng=2.3522) == "Europe/Paris"


def test_gps_to_tz_handles_ocean_does_not_crash():
    # Middle of the Pacific. timezonefinder may return None or an Etc/* zone.
    got = gps_to_tz(lat=0.0, lng=-160.0)
    assert got is None or got.startswith("Etc/") or got.startswith("Pacific/")


def test_compute_due_at_tokyo_3pm():
    # 2026-06-08 15:00 Tokyo = 2026-06-08 06:00 UTC
    got = compute_due_at(date(2026, 6, 8), time(15, 0), "Asia/Tokyo")
    assert got == datetime(2026, 6, 8, 6, 0, tzinfo=timezone.utc)


def test_compute_due_at_la_3pm_summer_dst():
    # 2026-06-08 15:00 LA (PDT, UTC-7) = 2026-06-08 22:00 UTC
    got = compute_due_at(date(2026, 6, 8), time(15, 0), "America/Los_Angeles")
    assert got == datetime(2026, 6, 8, 22, 0, tzinfo=timezone.utc)


def test_compute_due_at_la_3pm_winter_no_dst():
    # 2026-01-15 15:00 LA (PST, UTC-8) = 2026-01-15 23:00 UTC
    got = compute_due_at(date(2026, 1, 15), time(15, 0), "America/Los_Angeles")
    assert got == datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)
