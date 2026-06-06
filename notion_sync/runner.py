#!/usr/bin/env python3
"""Daily sync runner.

systemd fires this every hour. The runner checks whether the local time
in the configured timezone matches sync_global.sync_hour_local; if yes it
performs one sync pass. Otherwise it exits silently.

Single-side changes / new rows are synced silently — Sync Activity is
NOT touched (the data itself is the visible result). Conflicts (both
sides changed) and deletions (one side's ID disappeared) are detected
and enqueued to Sync Activity with decision=Pending. PR3 will apply
user-set decisions; PR2 only detects + enqueues.

Run manually for testing:
    python -m notion_sync.runner --force-now
    python -m notion_sync.runner --force-now --only trips
"""
from __future__ import annotations

import argparse
import json as _json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path
from zoneinfo import ZoneInfo

# notion_sync runs as a subprocess via `python -m notion_sync.runner`;
# add the parent dir to sys.path so `app` is importable. Phase 2 cleans
# this up by moving everything under app/.
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from app.io_utils import read_json_safe, write_json_atomic  # noqa: E402
from app.paths import DATA_DIR, SYNC_ALERT_STATE  # noqa: E402
from app.settings import settings  # noqa: E402

from notion_sync.activity import (
    frozen_pairs_for_collection,
    pending_action_exists,
    write_conflict,
    write_delete_question,
)
from notion_sync.changeset import (
    BothChanged,
    NoChange,
    NotionNew,
    NotionOnlyChange,
    NotionVanished,
    PbNew,
    PbOnlyChange,
    PbVanished,
    categorize,
)
from notion_sync.icons import icon_for
from notion_sync.logger import log_event
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.transform import (
    build_relation_lookup,
    collection_field_types,
    notion_page_to_pb_dict,
    pb_record_to_notion_props,
    relation_target_collections,
)


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
    `sync_hour_local` or `sync_hour_local_2` AND `paused` is False.

    Returns False if paused, bad timezone, or off-hour. Tolerant of
    missing config (defaults UTC, hour=3, no second slot).
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
    return local.hour in target_hours


def _pb_id_from_notion(page: dict) -> str:
    prop = page.get("properties", {}).get("pb_id", {})
    return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))


def _action_ids(a) -> tuple[str | None, str | None]:
    """Extract (pb_id, notion_id) for an Action, for the freeze check.

    Either side may be None if that side doesn't exist (e.g. PbNew has
    no notion_id yet; NotionVanished has a 'missing' notion_id stored
    on the PB row).
    """
    if isinstance(a, NoChange):
        return (a.pb_id, a.notion_id)
    if isinstance(a, PbOnlyChange):
        return (a.pb_row["id"], a.notion_id)
    if isinstance(a, NotionOnlyChange):
        return (a.pb_id, a.notion_page["id"])
    if isinstance(a, BothChanged):
        return (a.pb_row["id"], a.notion_page["id"])
    if isinstance(a, PbNew):
        return (a.pb_row["id"], None)
    if isinstance(a, NotionNew):
        return (None, a.notion_page["id"])
    if isinstance(a, NotionVanished):
        return (a.pb_row["id"], a.pb_row.get("notion_id") or None)
    if isinstance(a, PbVanished):
        return (_pb_id_from_notion(a.notion_page) or None, a.notion_page["id"])
    return (None, None)


def _apply_pb_to_notion(action: PbOnlyChange, *,
                        collection: str,
                        field_types: dict,
                        overrides_inv: dict,
                        title_field: str,
                        notion_schema: dict,
                        relation_lookup: dict | None,
                        relation_targets: dict | None,
                        pb: PBClient, nc: NotionClient) -> None:
    r = action.pb_row
    props = pb_record_to_notion_props(r, field_types, overrides_inv,
                                       title_field, notion_schema,
                                       relation_lookup=relation_lookup,
                                       relation_targets=relation_targets)
    props["last_synced_at"] = {"date": {"start": now_iso_date()}}
    page = nc.update_page(action.notion_id, properties=props,
                          icon=icon_for(collection, r))
    pb.update_record(collection, r["id"], {
        "notion_last_edited": page.get("last_edited_time"),
        "last_synced_at": now_iso_datetime(),
    })


def _apply_notion_to_pb(action: NotionOnlyChange, *,
                        collection: str,
                        field_types: dict,
                        overrides: dict,
                        title_field: str,
                        pb: PBClient, nc: NotionClient) -> None:
    npage = action.notion_page
    npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
    pb.update_record(collection, action.pb_id, npage_dict | {
        "notion_last_edited": npage.get("last_edited_time"),
        "last_synced_at": now_iso_datetime(),
    })


def _apply_pb_new(action: PbNew, *,
                  collection: str,
                  notion_db_id: str,
                  field_types: dict,
                  overrides_inv: dict,
                  title_field: str,
                  notion_schema: dict,
                  relation_lookup: dict | None,
                  relation_targets: dict | None,
                  pb: PBClient, nc: NotionClient) -> None:
    r = action.pb_row
    props = pb_record_to_notion_props(r, field_types, overrides_inv,
                                       title_field, notion_schema,
                                       relation_lookup=relation_lookup,
                                       relation_targets=relation_targets)
    props["pb_id"] = {"rich_text": [{"type": "text", "text": {"content": r["id"]}}]}
    props["last_synced_at"] = {"date": {"start": now_iso_date()}}
    page = nc.create_page(notion_db_id, props,
                          icon=icon_for(collection, r))
    pb.update_record(collection, r["id"], {
        "notion_id": page["id"],
        "notion_last_edited": page.get("last_edited_time"),
        "last_synced_at": now_iso_datetime(),
    })


def _apply_notion_new(action: NotionNew, *,
                      collection: str,
                      field_types: dict,
                      overrides: dict,
                      title_field: str,
                      pb: PBClient, nc: NotionClient) -> None:
    npage = action.notion_page
    npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
    created = pb.create_record(collection, npage_dict | {
        "notion_id": npage["id"],
        "notion_last_edited": npage.get("last_edited_time"),
        "last_synced_at": now_iso_datetime(),
    })
    nc.update_page(npage["id"], properties={
        "pb_id": {"rich_text": [{"type": "text",
                                  "text": {"content": created["id"]}}]},
        "last_synced_at": {"date": {"start": now_iso_date()}},
    })


def apply_pending_decisions(pb: PBClient, nc: NotionClient, *,
                            collection: str,
                            field_types: dict,
                            overrides: dict,
                            overrides_inv: dict,
                            title_field: str,
                            notion_schema: dict,
                            relation_lookup: dict | None = None,
                            relation_targets: dict | None = None) -> int:
    """Apply user-decided Sync Activity rows for this collection.

    Reads Sync Activity for rows where:
      - collection matches
      - applied_at is empty (not yet applied)
      - decision in {Use Notion, Use PB, Delete both, Keep both}

    Applies the decision (writes/deletes on the side(s) chosen), then
    marks applied_at on the Sync Activity row so subsequent runs don't
    re-apply. Errors per row are logged but do not abort the loop.

    Returns the number of decisions applied (incl. Keep both no-ops).
    """
    db_id = settings.notion_sync_activity_db_id
    filt = {"and": [
        {"property": "collection", "select": {"equals": collection}},
        {"property": "applied_at", "date":   {"is_empty": True}},
        {"or": [
            {"property": "decision", "select": {"equals": "Use Notion"}},
            {"property": "decision", "select": {"equals": "Use PB"}},
            {"property": "decision", "select": {"equals": "Delete both"}},
            {"property": "decision", "select": {"equals": "Keep both"}},
        ]},
    ]}
    rows = nc.query_database(db_id, filter_=filt)
    applied = 0
    for row in rows:
        try:
            _apply_one_decision(
                row, pb=pb, nc=nc, collection=collection,
                field_types=field_types, overrides=overrides,
                overrides_inv=overrides_inv, title_field=title_field,
                notion_schema=notion_schema,
                relation_lookup=relation_lookup,
                relation_targets=relation_targets,
            )
            nc.update_page(row["id"], properties={
                "applied_at": {"date": {"start": now_iso_date()}},
            })
            applied += 1
        except Exception as e:
            log_event("decision_apply_error",
                      collection=collection,
                      sa_row=row.get("id"),
                      error=str(e),
                      trace=traceback.format_exc()[:1000])
    return applied


def _apply_one_decision(row: dict, *, pb: PBClient, nc: NotionClient,
                        collection: str, field_types: dict,
                        overrides: dict, overrides_inv: dict,
                        title_field: str, notion_schema: dict,
                        relation_lookup: dict | None = None,
                        relation_targets: dict | None = None) -> None:
    p = row["properties"]
    decision = (p.get("decision", {}).get("select") or {}).get("name") or ""
    pb_id = "".join(rt.get("plain_text", "") for rt in p.get("pb_id", {}).get("rich_text", []))
    notion_id = "".join(rt.get("plain_text", "") for rt in p.get("notion_id", {}).get("rich_text", []))

    def _load_snap(prop_name: str) -> dict:
        s = "".join(rt.get("plain_text", "") for rt in p.get(prop_name, {}).get("rich_text", []))
        if not s:
            return {}
        try:
            return _json.loads(s)
        except _json.JSONDecodeError:
            return {}

    if decision == "Keep both":
        log_event("decision_applied", collection=collection,
                  decision=decision, pb_id=pb_id, notion_id=notion_id)
        return

    if decision == "Delete both":
        if pb_id:
            try:
                pb.delete_record(collection, pb_id)
            except Exception:
                pass   # already gone — treat as success
        if notion_id:
            try:
                nc.update_page(notion_id, archived=True)
            except Exception:
                pass   # already archived / missing — treat as success
        log_event("decision_applied", collection=collection,
                  decision=decision, pb_id=pb_id, notion_id=notion_id)
        return

    if decision == "Use Notion":
        notion_snap = _load_snap("notion_snapshot")
        if not pb_id or not notion_id or not notion_snap:
            raise RuntimeError("Use Notion requires both IDs + notion_snapshot")
        # Refresh the page's last_edited_time so subsequent runs see NoChange.
        try:
            current_page = nc.retrieve_page(notion_id)
            notion_last_edited = current_page.get("last_edited_time", "")
        except Exception:
            notion_last_edited = ""
        pb.update_record(collection, pb_id, notion_snap | {
            "notion_last_edited": notion_last_edited,
            "last_synced_at": now_iso_datetime(),
        })
        log_event("decision_applied", collection=collection,
                  decision=decision, pb_id=pb_id, notion_id=notion_id)
        return

    if decision == "Use PB":
        pb_snap = _load_snap("pb_snapshot")
        if not pb_id or not notion_id or not pb_snap:
            raise RuntimeError("Use PB requires both IDs + pb_snapshot")
        props = pb_record_to_notion_props(
            pb_snap, field_types, overrides_inv, title_field, notion_schema,
            relation_lookup=relation_lookup,
            relation_targets=relation_targets,
        )
        props["last_synced_at"] = {"date": {"start": now_iso_date()}}
        page = nc.update_page(notion_id, properties=props,
                              icon=icon_for(collection, pb_snap))
        pb.update_record(collection, pb_id, {
            "notion_last_edited": page.get("last_edited_time", ""),
            "last_synced_at": now_iso_datetime(),
        })
        log_event("decision_applied", collection=collection,
                  decision=decision, pb_id=pb_id, notion_id=notion_id)
        return

    # Merge or unknown — leave un-applied; user must handle manually.
    raise RuntimeError(f"decision not auto-appliable: {decision!r}")


def sync_collection(cfg_row: dict, pb: PBClient, nc: NotionClient) -> dict:
    collection = cfg_row["collection"]
    notion_db_id = cfg_row["notion_db_id"]
    overrides = cfg_row.get("field_map_overrides") or {}
    overrides_inv = {v: k for k, v in overrides.items()}
    last_synced_at = cfg_row.get("last_synced_at") or ""

    field_types = collection_field_types(pb, collection)
    title_field = cfg_row.get("title_field") or ""
    if not title_field:
        raise RuntimeError(
            f"sync_config[{collection}].title_field is empty — set it via "
            f"the settings UI or PB admin before this collection can sync"
        )

    notion_db = nc.retrieve_database(notion_db_id)
    notion_schema = notion_db.get("properties", {})

    # Build PB→Notion relation lookup once per sync_collection call. Covers
    # every currently-enabled sync target. Fresh rows added DURING this
    # pass don't appear in the lookup, but they'll be linkable on the next
    # pass — acceptable for initial relation backfill.
    all_targets = pb.list_records("sync_config", filter="enabled=true", sort="")
    target_names = [t["collection"] for t in all_targets]
    relation_lookup = build_relation_lookup(pb, target_names)
    relation_targets = relation_target_collections(pb, collection)

    # Phase 0: apply user-decided Sync Activity rows. After this, applied
    # rows have applied_at set and won't appear in the freeze set below.
    decisions_applied = apply_pending_decisions(
        pb, nc, collection=collection,
        field_types=field_types, overrides=overrides,
        overrides_inv=overrides_inv, title_field=title_field,
        notion_schema=notion_schema,
        relation_lookup=relation_lookup,
        relation_targets=relation_targets,
    )

    pb_rows = pb.list_records(collection, sort="")
    notion_rows = nc.query_database(notion_db_id)
    actions = categorize(pb_rows, notion_rows, last_synced_at=last_synced_at)

    # Freeze: rows with a Pending Conflict or Delete? in Sync Activity
    # are off-limits until the user picks a decision. The runner skips
    # any action whose pb_id or notion_id appears in either set, no
    # matter what category it falls into. Prevents subsequent edits
    # from cascading into NotionOnlyChange / PbOnlyChange / etc and
    # silently overwriting the conflicted side before the user decides.
    frozen_pb_ids, frozen_notion_ids = frozen_pairs_for_collection(
        nc, collection=collection,
    )

    # Counts are tallied AFTER the freeze check so frozen rows don't
    # inflate applied/conflict/delete counts in the log.
    counts: dict[str, int] = {}
    skipped_frozen = 0

    for a in actions:
        try:
            pid, nid = _action_ids(a)
            if (pid and pid in frozen_pb_ids) or (nid and nid in frozen_notion_ids):
                skipped_frozen += 1
                continue
            counts[type(a).__name__] = counts.get(type(a).__name__, 0) + 1
            if isinstance(a, NoChange):
                continue
            elif isinstance(a, PbOnlyChange):
                _apply_pb_to_notion(a, collection=collection,
                                     field_types=field_types,
                                     overrides_inv=overrides_inv,
                                     title_field=title_field,
                                     notion_schema=notion_schema,
                                     relation_lookup=relation_lookup,
                                     relation_targets=relation_targets,
                                     pb=pb, nc=nc)
            elif isinstance(a, NotionOnlyChange):
                _apply_notion_to_pb(a, collection=collection,
                                     field_types=field_types,
                                     overrides=overrides,
                                     title_field=title_field,
                                     pb=pb, nc=nc)
            elif isinstance(a, PbNew):
                _apply_pb_new(a, collection=collection,
                               notion_db_id=notion_db_id,
                               field_types=field_types,
                               overrides_inv=overrides_inv,
                               title_field=title_field,
                               notion_schema=notion_schema,
                               relation_lookup=relation_lookup,
                               relation_targets=relation_targets,
                               pb=pb, nc=nc)
            elif isinstance(a, NotionNew):
                _apply_notion_new(a, collection=collection,
                                   field_types=field_types,
                                   overrides=overrides,
                                   title_field=title_field,
                                   pb=pb, nc=nc)
            elif isinstance(a, BothChanged):
                # First detection only — re-detection is short-circuited
                # by the outer freeze check above.
                pb_id = a.pb_row["id"]
                notion_id = a.notion_page["id"]
                notion_dict = notion_page_to_pb_dict(
                    a.notion_page, field_types, overrides,
                )
                write_conflict(
                    nc,
                    collection=collection,
                    summary=str(a.pb_row.get(title_field, ""))[:120],
                    pb_id=pb_id, notion_id=notion_id,
                    pb_snapshot=a.pb_row,
                    notion_snapshot=notion_dict,
                    record_link=a.notion_page.get("url"),
                )
            elif isinstance(a, NotionVanished):
                # First detection only.
                pb_id = a.pb_row["id"]
                missing_nid = a.pb_row.get("notion_id") or ""
                write_delete_question(
                    nc,
                    collection=collection,
                    summary=("Notion page missing: "
                             + str(a.pb_row.get(title_field, ""))[:80]),
                    pb_id=pb_id, notion_id=missing_nid,
                    snapshot=a.pb_row,
                )
            elif isinstance(a, PbVanished):
                # First detection only.
                missing_pid = _pb_id_from_notion(a.notion_page)
                notion_id = a.notion_page["id"]
                notion_dict = notion_page_to_pb_dict(
                    a.notion_page, field_types, overrides,
                )
                write_delete_question(
                    nc,
                    collection=collection,
                    summary=("PB record missing: "
                             + str(notion_dict.get(title_field, ""))[:80]),
                    pb_id=missing_pid, notion_id=notion_id,
                    snapshot=notion_dict,
                )
        except Exception as e:
            log_event("apply_error",
                      collection=collection,
                      action=type(a).__name__,
                      error=str(e),
                      trace=traceback.format_exc()[:1000])

    return {
        "counts": counts,
        "applied": (counts.get("PbOnlyChange", 0)
                    + counts.get("NotionOnlyChange", 0)
                    + counts.get("PbNew", 0)
                    + counts.get("NotionNew", 0)),
        "conflicts": counts.get("BothChanged", 0),
        "deletes": counts.get("NotionVanished", 0) + counts.get("PbVanished", 0),
        "frozen_skipped": skipped_frozen,
        "decisions_applied": decisions_applied,
    }


def main() -> int:
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

    overall: dict[str, int] = {"applied": 0, "conflicts": 0, "deletes": 0}
    for t in targets:
        try:
            result = sync_collection(t, pb, nc)
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
            log_event("collection_error",
                      collection=t["collection"],
                      error=str(e),
                      trace=traceback.format_exc()[:2000])

    if sync_global.get("id"):
        pb.update_record("sync_global", sync_global["id"], {
            "last_run_at": now_iso_datetime(),
        })

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


def cleanup_resolved_activity(nc: NotionClient, *, days: int = 90) -> int:
    """Archive Sync Activity rows whose applied_at is older than `days`.

    Keeps the queue table small over time. Archives (not hard-deletes)
    so the user can un-archive in Notion to recover a row if needed.
    Returns the number archived.
    """
    db_id = settings.notion_sync_activity_db_id
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = nc.query_database(db_id, filter_={"and": [
        {"property": "applied_at", "date": {"is_not_empty": True}},
        {"property": "applied_at", "date": {"before": cutoff}},
    ]})
    archived = 0
    for r in rows:
        try:
            nc.update_page(r["id"], archived=True)
            archived += 1
        except Exception as e:
            log_event("cleanup_error", page=r.get("id"), error=str(e))
    return archived


_ALERT_STATE_FILE = "sync_alert_state.json"
_ALERT_DEDUPE_SECONDS = 6 * 3600   # 6 hours


def notify_pending(nc: NotionClient) -> int:
    """Create a Phone Bridge chat session listing Pending Sync Activity rows.

    Same UX as the weekly report: the session appears in the sidebar so
    the next time the user opens Phone Bridge they see it. Tap → talk
    to Claude inline ("帮我看看这条冲突应该选哪个").

    Dedupe: only create a new session if the last one was created more
    than 6 hours ago OR the pending row-id set has changed since the
    last alert. State lives in .bridge_data/sync_alert_state.json.

    Returns the Pending count regardless of whether a session was made.
    """
    db_id = settings.notion_sync_activity_db_id
    rows = nc.query_database(db_id, filter_={"and": [
        {"property": "decision",   "select": {"equals": "Pending"}},
        {"property": "applied_at", "date":   {"is_empty": True}},
    ]})
    n = len(rows)
    if n == 0:
        return 0

    current_ids = sorted(r["id"] for r in rows)
    if _alert_already_sent(current_ids):
        log_event("alert_skipped", reason="recent + same set", pending=n)
        return n

    title = f"📋 同步待确认 {n} 项"
    md = _render_pending_markdown(rows)

    try:
        # Lazy import — keep runner usable in environments where
        # the phone-bridge db module isn't already on sys.path.
        import db  # type: ignore
        # The bridge sqlite path matches server.py's wiring.
        db.init(DATA_DIR / "bridge.db")
        sid = db.create_session(
            cwd=settings.default_cwd or "/home/dev",
            title=title[:80], mode="chat", model="",
        )
        db.append_message(sid, "assistant_text", {"text": md})
        log_event("alert_session_created", session_id=sid, pending=n)
        _save_alert_state(current_ids)
    except Exception as e:
        log_event("alert_failed", reason=str(e), pending=n)
    return n


def _render_pending_markdown(rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"## 📋 同步待确认 {len(rows)} 项")
    lines.append("")
    lines.append("以下条目两边数据不一致(或一边消失了),需要你裁决:")
    lines.append("")
    by_op: dict[str, list[dict]] = {}
    for r in rows:
        p = r.get("properties", {})
        op = (p.get("op", {}).get("select") or {}).get("name", "?")
        by_op.setdefault(op, []).append(r)
    op_label = {
        "Conflict":           "🔀 冲突(两边都改了同一字段)",
        "Delete?":            "🗑️ 删除?(一边的记录消失了)",
        "Possible duplicate": "👯 可能重复(初次对齐发现)",
        "Schema mismatch":    "🧬 字段对不上",
    }
    for op, items in by_op.items():
        lines.append(f"### {op_label.get(op, op)} — {len(items)} 项")
        lines.append("")
        for r in items:
            p = r.get("properties", {})
            coll = (p.get("collection", {}).get("select") or {}).get("name", "?")
            summ = "".join(rt.get("plain_text", "")
                            for rt in p.get("summary", {}).get("rich_text", []))
            link = r.get("url") or ""
            lines.append(f"- **{coll}** · {summ}")
            if link:
                lines.append(f"  - [打开 Sync Activity 那一行]({link})")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**怎么处理:** 打开 Sync Activity DB(用上面任意链接),把每行的 "
                  "`Decision` 改成 `Use Notion` / `Use PB` / `Delete both` / `Keep both`。"
                  "下一次同步(每天 03:00 ET,或叫 Claude `同步一下`)会自动执行你的选择。")
    return "\n".join(lines)


def _alert_state_path() -> str:
    # Kept for back-compat with anything that imports it; new code uses
    # app.paths.SYNC_ALERT_STATE directly.
    return str(SYNC_ALERT_STATE)


def _alert_already_sent(current_ids: list[str]) -> bool:
    state = read_json_safe(SYNC_ALERT_STATE, default=None)
    if not state:
        return False
    last_ts = float(state.get("last_alert_ts") or 0)
    last_ids = state.get("last_pending_ids") or []
    now = datetime.now(timezone.utc).timestamp()
    same_set = list(current_ids) == list(last_ids)
    fresh = (now - last_ts) < _ALERT_DEDUPE_SECONDS
    return fresh and same_set


def _save_alert_state(current_ids: list[str]) -> None:
    state = {
        "last_alert_ts":     datetime.now(timezone.utc).timestamp(),
        "last_pending_ids":  list(current_ids),
    }
    try:
        write_json_atomic(SYNC_ALERT_STATE, state)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
