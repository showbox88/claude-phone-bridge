"""should_run_now tests — pure function with no I/O."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.runner import should_run_now


def _utc(year, month, day, hour):
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def test_runs_at_configured_hour_in_local_tz():
    # 07:00 UTC == 03:00 America/New_York (EDT, summer)
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7)) is True


def test_does_not_run_off_hour():
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 8)) is False
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 6)) is False


def test_respects_paused():
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3, "paused": True}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7)) is False


def test_handles_tokyo():
    # 18:00 UTC == 03:00 Asia/Tokyo (JST = UTC+9)
    cfg = {"timezone": "Asia/Tokyo", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 18)) is True
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 19)) is False


def test_handles_missing_config_safely():
    # Defaults: UTC + sync_hour_local=3
    cfg = {}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 3)) is True
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 4)) is False


def test_bad_timezone_returns_false():
    cfg = {"timezone": "Mars/Olympus", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7)) is False


def test_daylight_savings_us_winter():
    # In winter, America/New_York is EST = UTC-5, so 03:00 local == 08:00 UTC
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 1, 15, 8)) is True
    assert should_run_now(cfg, now_utc=_utc(2026, 1, 15, 7)) is False


def test_runs_at_either_of_two_hours():
    # EDT: 03:00 local = 07:00 UTC; 15:00 local = 19:00 UTC. Both fire.
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3,
           "sync_hour_local_2": 15, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7))  is True
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 19)) is True
    # Off-hours in between should still skip.
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 12)) is False


def test_second_hour_alone_works():
    # Only sync_hour_local_2 set; the first slot is empty/null.
    cfg = {"timezone": "America/New_York", "sync_hour_local": "",
           "sync_hour_local_2": 15, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 19)) is True
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7))  is False


def test_invalid_second_hour_ignored():
    # Bogus value in second slot — silently dropped, first hour still wins.
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3,
           "sync_hour_local_2": "abc", "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7))  is True
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 19)) is False


def test_paused_overrides_both_hours():
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3,
           "sync_hour_local_2": 15, "paused": True}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7))  is False
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 19)) is False


def test_should_run_now_false_within_23h_of_last_run():
    """Same-hour double-run within 23h should be blocked (e.g. quick
    service restart that lands within the same sync_hour_local window)."""
    now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
    last = (now - timedelta(hours=2)).isoformat()
    cfg = {"sync_hour_local": 9, "timezone": "UTC",
           "last_successful_run_at": last}
    assert should_run_now(cfg, now_utc=now) is False


def test_should_run_now_true_after_23h_gap():
    """After a full day, the gate releases and the runner runs again."""
    now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
    last = (now - timedelta(hours=24)).isoformat()
    cfg = {"sync_hour_local": 9, "timezone": "UTC",
           "last_successful_run_at": last}
    assert should_run_now(cfg, now_utc=now) is True


def test_should_run_now_true_no_last_run_recorded():
    """First-ever run: no last_successful_run_at → run if hour matches."""
    now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
    cfg = {"sync_hour_local": 9, "timezone": "UTC"}
    assert should_run_now(cfg, now_utc=now) is True


def test_should_run_now_true_with_malformed_last_run():
    """Malformed last_successful_run_at is treated as 'no last run' so
    the gate doesn't permanently brick sync if PB writes a bad value."""
    now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
    cfg = {"sync_hour_local": 9, "timezone": "UTC",
           "last_successful_run_at": "not-a-date"}
    assert should_run_now(cfg, now_utc=now) is True


def test_should_run_now_handles_trailing_z_iso_format():
    """PB serializes datetimes as 'YYYY-MM-DD HH:MM:SS.sssZ' — parser
    must accept that."""
    now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
    cfg = {"sync_hour_local": 9, "timezone": "UTC",
           "last_successful_run_at": "2026-06-09T07:30:00.000Z"}
    # 2h ago — gate should block.
    assert should_run_now(cfg, now_utc=now) is False
