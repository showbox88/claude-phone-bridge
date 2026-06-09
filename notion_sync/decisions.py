"""Apply user-set decisions from Sync Activity rows.

When the runner detects a Conflict / Delete? on a row, it writes a
Pending row to the Notion Sync Activity DB. The user picks `Use Notion`
/ `Use PB` / `Delete both` / `Keep both` in Notion. On the next sync
pass, `apply_pending_decisions` reads those rows and executes the
chosen action.

Phase 5 Task 14 split this out of runner.py. The Phase 5 Task 3 race
fix (Use Notion PATCHes Notion FIRST so last_edited_time advances)
lives here unchanged.
"""
from __future__ import annotations

import json as _json
import traceback

from app.settings import settings

from notion_sync.icons import icon_for
from notion_sync.logger import log_event
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.transform import pb_record_to_notion_props


def _now_iso_date() -> str:
    from notion_sync.bootstrap import now_iso_date
    return now_iso_date()


def _now_iso_datetime() -> str:
    from notion_sync.bootstrap import now_iso_datetime
    return now_iso_datetime()


def apply_pending_decisions(pb: PBClient, nc: NotionClient, *,
                            collection: str,
                            field_types: dict,
                            overrides: dict,
                            overrides_inv: dict,
                            title_field: str,
                            notion_schema: dict,
                            relation_lookup: dict | None = None,
                            relation_targets: dict | None = None,
                            icon_field: str | None = None,
                            icon_default: str | None = None) -> int:
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
                icon_field=icon_field,
                icon_default=icon_default,
            )
            nc.update_page(row["id"], properties={
                "applied_at": {"date": {"start": _now_iso_date()}},
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
                        relation_targets: dict | None = None,
                        icon_field: str | None = None,
                        icon_default: str | None = None) -> None:
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
        # PATCH Notion's last_synced_at FIRST so its last_edited_time
        # advances. Reading retrieve_page() without patching first leaves
        # PB with the old timestamp; the next sync sees PB ahead of Notion
        # and flags a false conflict (Phase 5 Task 3 fix).
        try:
            page = nc.update_page(notion_id, properties={
                "last_synced_at": {"date": {"start": _now_iso_date()}},
            })
            notion_last_edited = page.get("last_edited_time", "")
        except Exception:
            notion_last_edited = ""
        pb.update_record(collection, pb_id, notion_snap | {
            "notion_last_edited": notion_last_edited,
            "last_synced_at": _now_iso_datetime(),
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
        props["last_synced_at"] = {"date": {"start": _now_iso_date()}}
        page = nc.update_page(notion_id, properties=props,
                              icon=icon_for(collection, pb_snap,
                                            icon_field=icon_field,
                                            icon_default=icon_default))
        pb.update_record(collection, pb_id, {
            "notion_last_edited": page.get("last_edited_time", ""),
            "last_synced_at": _now_iso_datetime(),
        })
        log_event("decision_applied", collection=collection,
                  decision=decision, pb_id=pb_id, notion_id=notion_id)
        return

    # Merge or unknown — leave un-applied; user must handle manually.
    raise RuntimeError(f"decision not auto-appliable: {decision!r}")
