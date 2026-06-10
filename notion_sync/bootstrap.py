#!/usr/bin/env python3
"""Daily sync runner entry-point.

systemd fires `python -m notion_sync.runner` every hour. The runner
checks whether the local time in the configured timezone matches
sync_global.sync_hour_local; if yes it performs one sync pass.
Otherwise it exits silently.

Single-side changes / new rows are synced silently — Sync Activity is
NOT touched (the data itself is the visible result). Conflicts (both
sides changed) and deletions (one side's ID disappeared) are detected
and enqueued to Sync Activity with decision=Pending.

Run manually for testing:
    python -m notion_sync.runner --force-now
    python -m notion_sync.runner --force-now --only trips

Phase 5 Task 14 split the original runner.py into this bootstrap +
dispatch + decisions + post_phases. runner.py is now a thin shim.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path
from zoneinfo import ZoneInfo

# notion_sync runs as a subprocess via `python -m notion_sync.runner`;
# add the parent dir to sys.path so `app` is importable. Phase 2 cleans
# this up by moving everything under app/.
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from notion_sync.activity import frozen_pairs_for_all  # noqa: E402
from notion_sync.logger import log_event  # noqa: E402
from notion_sync.notion_api import NotionClient  # noqa: E402
from notion_sync.pb_api import PBClient  # noqa: E402


def now_iso_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_iso_datetime() -> str:
    """Match PB's autodate format exactly: 'YYYY-MM-DD HH:MM:SS.SSSZ'.

    Without milliseconds + Z, PB normalizes our shorter string to '.000Z'
    on storage, and string-comparing it against PB-autodate values (which
    carry real ms) produces wrong results — pb.updated always looks
    'greater' than our sync_config.last_synced_at, triggering perpetual
    PbOnlyChange even when nothing changed.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 1000:03d}Z"


def should_run_now(sync_global: dict, *, now_utc: datetime | None = None) -> bool:
    """True iff the local hour in sync_global.timezone matches EITHER
    `sync_hour_local` or `sync_hour_local_2` AND `paused` is False AND
    ≥23h have elapsed since `last_successful_run_at`.

    The ≥23h gate prevents double-run within the same wall-clock day
    (e.g. quick service restart) and tolerates clock drift across hour
    boundaries (NTP can move local time by several seconds). A
    missing/malformed `last_successful_run_at` is treated as "no last
    run" and does not block.

    Returns False if paused, bad timezone, off-hour, or within 23h of
    the last successful run. Tolerant of missing config (defaults UTC,
    hour=3, no second slot).
    """
    if sync_global.get("paused"):
        return False
    tz_name = sync_global.get("timezone") or "UTC"
    target_hours: set[int] = set()
    for key in ("sync_hour_local", "sync_hour_local_2"):
        raw = sync_global.get(key)
        if raw is None or raw == "":
            continue
        try:
            h = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= h <= 23:
            target_hours.add(h)
    if not target_hours:
        target_hours = {3}     # safety default if config is empty
    now = now_utc or datetime.now(timezone.utc)
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:
        log_event("bad_timezone", configured=tz_name)
        return False
    if local.hour not in target_hours:
        return False
    last_run = sync_global.get("last_successful_run_at")
    if last_run:
        try:
            # PB datetime fields use ISO 8601 (possibly with trailing 'Z').
            last_dt = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if now - last_dt < timedelta(hours=23):
                return False
        except (ValueError, AttributeError, TypeError):
            pass  # malformed timestamp — treat as "no last run" and proceed
    return True


def main() -> int:
    # Imports deferred so test_runner_guard (which only needs
    # should_run_now) doesn't drag in dispatch/post_phases / their
    # transitive imports (db, app.settings, etc).
    from notion_sync.dispatch import sync_collection
    from notion_sync.post_phases import (
        cleanup_resolved_activity,
        notify_pending,
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--force-now", action="store_true",
                    help="Bypass the time guard (still respects paused flag)")
    ap.add_argument("--only", help="Restrict to one collection")
    args = ap.parse_args()

    pb = PBClient()
    nc = NotionClient()

    globals_ = pb.list_records("sync_global", sort="")
    sync_global = globals_[0] if globals_ else {}

    if not args.force_now:
        if not should_run_now(sync_global):
            return 0
    elif sync_global.get("paused"):
        log_event("skipped_paused")
        return 0

    log_event("run_start", forced=args.force_now)

    targets = pb.list_records("sync_config", filter="enabled=true", sort="")
    if args.only:
        targets = [t for t in targets if t["collection"] == args.only]
        if not targets:
            log_event("run_aborted", reason=f"no sync_config for {args.only}")
            return 1

    # Pre-fetch frozen pairs for every enabled collection in ONE Notion
    # query — replaces N sequential per-collection queries inside the
    # sync_collection loop. At Task 0 baseline (10 enabled collections)
    # this saves 9 round-trips per run. The mapping is keyed by
    # collection name; sync_collection accepts the per-collection slice
    # via its frozen_pairs kwarg.
    frozen_all = frozen_pairs_for_all(
        nc, collections=[t["collection"] for t in targets],
    )

    overall: dict[str, int] = {"applied": 0, "conflicts": 0, "deletes": 0}
    any_collection_error = False
    for t in targets:
        try:
            result = sync_collection(
                t, pb, nc,
                frozen_pairs=frozen_all.get(t["collection"], (set(), set())),
            )
            for k in ("applied", "conflicts", "deletes"):
                overall[k] += result.get(k, 0)
            pb.update_record("sync_config", t["id"], {
                "last_synced_at": now_iso_datetime(),
                "last_sync_summary": (
                    f"runner: applied={result['applied']} "
                    f"conflicts={result['conflicts']} deletes={result['deletes']}"
                ),
            })
            log_event("collection_done",
                      collection=t["collection"],
                      **result)
        except Exception as e:
            any_collection_error = True
            log_event("collection_error",
                      collection=t["collection"],
                      error=str(e),
                      trace=traceback.format_exc()[:2000])

    if sync_global.get("id"):
        update_fields: dict = {"last_run_at": now_iso_datetime()}
        # Mark this run as successful so the ≥23h gate can advance.
        # Only stamp on the happy path — a per-collection failure means
        # we want the next hourly tick (same hour) to retry.
        if not any_collection_error:
            update_fields["last_successful_run_at"] = now_iso_datetime()
        try:
            pb.update_record("sync_global", sync_global["id"], update_fields)
        except Exception as e:
            log_event("last_run_update_failed", error=str(e))

    # Date-based Notion linkages (Day↔Stops, Day↔Trip, Stop↔Trip).
    # Linkages derive from Notion-side dates only — independent of PB
    # relation field values. Idempotent: runs every sync pass but only
    # PATCHes pages where the date-computed target differs.
    try:
        from notion_sync.linkage import update_date_linkages
        cfg_rows = pb.list_records("sync_config", sort="")
        rows_by_coll = {r["collection"]: r for r in cfg_rows}
        d = rows_by_coll.get("days")
        s = rows_by_coll.get("stops")
        t = rows_by_coll.get("trips")
        if d and s and t and d.get("enabled") and s.get("enabled") and t.get("enabled"):
            linkage_counts = update_date_linkages(
                nc,
                days_db_id=d["notion_db_id"],
                stops_db_id=s["notion_db_id"],
                trips_db_id=t["notion_db_id"],
                days_overrides=d.get("field_map_overrides") or {},
                stops_overrides=s.get("field_map_overrides") or {},
                trips_overrides=t.get("field_map_overrides") or {},
            )
            overall.update(linkage_counts)
            log_event("linkages_updated", **linkage_counts)
        else:
            log_event("linkages_skipped", reason="days/stops/trips not all enabled")
    except Exception as e:
        log_event("linkage_error", error=str(e),
                  trace=traceback.format_exc()[:500])

    # Notify the user if there are Pending Sync Activity rows that need
    # their attention. Best-effort: failure to import push or send
    # doesn't break the run.
    try:
        pending_count = notify_pending(nc)
        overall["pending"] = pending_count
    except Exception as e:
        log_event("notify_error", error=str(e),
                  trace=traceback.format_exc()[:500])

    # Archive resolved Sync Activity rows older than 90 days.
    try:
        archived = cleanup_resolved_activity(nc, days=90)
        if archived:
            overall["archived_resolved"] = archived
            log_event("cleanup_archived", count=archived)
    except Exception as e:
        log_event("cleanup_error", error=str(e),
                  trace=traceback.format_exc()[:500])

    log_event("run_end", **overall)
    return 0
