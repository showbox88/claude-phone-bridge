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
import sys
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from notion_sync.activity import (
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
from notion_sync.logger import log_event
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.transform import (
    collection_field_types,
    notion_page_to_pb_dict,
    pb_record_to_notion_props,
)


TITLE_FIELD_BY_COLLECTION = {
    "trips": "title", "plans": "title", "todos": "title",
    "days":  "name",  "contacts": "name", "locations": "name",
}


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
    """True iff the local hour in sync_global.timezone == sync_hour_local
    AND sync_global.paused is False. Returns False if paused or off-hour.
    Tolerant of missing config (defaults UTC, hour=3).
    """
    if sync_global.get("paused"):
        return False
    tz_name = sync_global.get("timezone") or "UTC"
    target_hour = int(sync_global.get("sync_hour_local") or 3)
    now = now_utc or datetime.now(timezone.utc)
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:
        log_event("bad_timezone", configured=tz_name)
        return False
    return local.hour == target_hour


def _pb_id_from_notion(page: dict) -> str:
    prop = page.get("properties", {}).get("pb_id", {})
    return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))


def _apply_pb_to_notion(action: PbOnlyChange, *,
                        collection: str,
                        field_types: dict,
                        overrides_inv: dict,
                        title_field: str,
                        notion_schema: dict,
                        pb: PBClient, nc: NotionClient) -> None:
    r = action.pb_row
    props = pb_record_to_notion_props(r, field_types, overrides_inv,
                                       title_field, notion_schema)
    props["last_synced_at"] = {"date": {"start": now_iso_date()}}
    page = nc.update_page(action.notion_id, properties=props)
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
                  pb: PBClient, nc: NotionClient) -> None:
    r = action.pb_row
    props = pb_record_to_notion_props(r, field_types, overrides_inv,
                                       title_field, notion_schema)
    props["pb_id"] = {"rich_text": [{"type": "text", "text": {"content": r["id"]}}]}
    props["last_synced_at"] = {"date": {"start": now_iso_date()}}
    page = nc.create_page(notion_db_id, props)
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


def sync_collection(cfg_row: dict, pb: PBClient, nc: NotionClient) -> dict:
    collection = cfg_row["collection"]
    notion_db_id = cfg_row["notion_db_id"]
    overrides = cfg_row.get("field_map_overrides") or {}
    overrides_inv = {v: k for k, v in overrides.items()}
    last_synced_at = cfg_row.get("last_synced_at") or ""

    field_types = collection_field_types(pb, collection)
    title_field = TITLE_FIELD_BY_COLLECTION.get(collection, "title")

    notion_db = nc.retrieve_database(notion_db_id)
    notion_schema = notion_db.get("properties", {})

    pb_rows = pb.list_records(collection, sort="")
    notion_rows = nc.query_database(notion_db_id)
    actions = categorize(pb_rows, notion_rows, last_synced_at=last_synced_at)

    counts: dict[str, int] = {}
    for a in actions:
        counts[type(a).__name__] = counts.get(type(a).__name__, 0) + 1

    for a in actions:
        try:
            if isinstance(a, NoChange):
                continue
            elif isinstance(a, PbOnlyChange):
                _apply_pb_to_notion(a, collection=collection,
                                     field_types=field_types,
                                     overrides_inv=overrides_inv,
                                     title_field=title_field,
                                     notion_schema=notion_schema,
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
                               pb=pb, nc=nc)
            elif isinstance(a, NotionNew):
                _apply_notion_new(a, collection=collection,
                                   field_types=field_types,
                                   overrides=overrides,
                                   title_field=title_field,
                                   pb=pb, nc=nc)
            elif isinstance(a, BothChanged):
                pb_id = a.pb_row["id"]
                notion_id = a.notion_page["id"]
                if pending_action_exists(nc, op="Conflict",
                                          pb_id=pb_id, notion_id=notion_id):
                    continue
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
                pb_id = a.pb_row["id"]
                missing_nid = a.pb_row.get("notion_id") or ""
                if pending_action_exists(nc, op="Delete?",
                                          pb_id=pb_id, notion_id=missing_nid):
                    continue
                write_delete_question(
                    nc,
                    collection=collection,
                    summary=("Notion page missing: "
                             + str(a.pb_row.get(title_field, ""))[:80]),
                    pb_id=pb_id, notion_id=missing_nid,
                    snapshot=a.pb_row,
                )
            elif isinstance(a, PbVanished):
                missing_pid = _pb_id_from_notion(a.notion_page)
                notion_id = a.notion_page["id"]
                if pending_action_exists(nc, op="Delete?",
                                          pb_id=missing_pid, notion_id=notion_id):
                    continue
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

    log_event("run_end", **overall)
    return 0


if __name__ == "__main__":
    sys.exit(main())
