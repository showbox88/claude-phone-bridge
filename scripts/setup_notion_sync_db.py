#!/usr/bin/env python3
"""One-time bootstrap for Notion ↔ PB sync (PR1).

Does:
  1. Add pb_id + last_synced_at columns to each of the 6 sync-target
     Notion DBs (idempotent — checks for existing properties first).
  2. Create the Sync Activity Notion DB under a parent page.
  3. Seed sync_config rows in PB (one per sync target).

Run:
    python3 scripts/setup_notion_sync_db.py --parent-page-id <UUID>
    # or set NOTION_SYNC_PARENT_PAGE_ID in .env
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient


# Notion DB ID for each sync-target PB collection.
# Copied from pocketbase/migrate_notion.py's DBS list (only the 6 we sync).
SYNC_TARGETS: dict[str, str] = {
    "trips":     "df7ea062-7b18-4c4f-98f1-bfec8258c3db",
    "days":      "13329dea-4f55-4fc8-8e64-6c1ff19353bb",
    "plans":     "c951c7a9-a8f5-4ffd-aea2-1244e437ae46",
    "todos":     "5d4e3f93-cf13-4707-97c5-59b38940baac",
    "contacts":  "e304a6c3-4771-4c69-9ffc-97a672a1ac0c",
    "locations": "257c34c1-ac50-455d-9c8a-8d810de5c1e5",
}


SYNC_ACTIVITY_PROPERTIES = {
    "title":           {"title": {}},
    "op":              {"select": {"options": [
        {"name": "Auto-applied"}, {"name": "Conflict"},
        {"name": "Delete?"},     {"name": "Possible duplicate"},
        {"name": "Schema mismatch"},
    ]}},
    "direction":       {"select": {"options": [
        {"name": "Notion→PB"}, {"name": "PB→Notion"},
        {"name": "Both"},      {"name": "None"},
    ]}},
    "collection":      {"select": {"options": [
        {"name": c} for c in SYNC_TARGETS
    ]}},
    "record_link":     {"url": {}},
    "pb_id":           {"rich_text": {}},
    "notion_id":       {"rich_text": {}},
    "summary":         {"rich_text": {}},
    "pb_snapshot":     {"rich_text": {}},
    "notion_snapshot": {"rich_text": {}},
    "decision":        {"select": {"options": [
        {"name": "Pending"},     {"name": "Use Notion"},
        {"name": "Use PB"},      {"name": "Delete both"},
        {"name": "Keep both"},   {"name": "Merge"},
        {"name": "N/A"},
    ]}},
    "detected_at":     {"date": {}},
    "applied_at":      {"date": {}},
    "notes":           {"rich_text": {}},
}


def add_pipeline_columns(nc: NotionClient, db_id: str) -> None:
    db = nc.retrieve_database(db_id)
    existing = set(db.get("properties", {}).keys())
    patch: dict = {}
    if "pb_id" not in existing:
        patch["pb_id"] = {"rich_text": {}}
    if "last_synced_at" not in existing:
        patch["last_synced_at"] = {"date": {}}
    if not patch:
        print(f"  [skip] {db_id}: pipeline columns already present")
        return
    nc.update_database(db_id, {"properties": patch})
    print(f"  [ok]   {db_id}: added {list(patch.keys())}")


def find_or_create_activity_db(nc: NotionClient, parent_page_id: str) -> str:
    existing = os.environ.get("NOTION_SYNC_ACTIVITY_DB_ID")
    if existing:
        try:
            nc.retrieve_database(existing)
            print(f"  [skip] activity DB already configured: {existing}")
            return existing
        except RuntimeError:
            print(f"  [warn] NOTION_SYNC_ACTIVITY_DB_ID={existing} not found, creating new")

    db = nc.create_database(parent_page_id, "Sync Activity", SYNC_ACTIVITY_PROPERTIES)
    print(f"  [ok]   created Sync Activity DB: {db['id']}")
    print(f"         ADD TO .env: NOTION_SYNC_ACTIVITY_DB_ID={db['id']}")
    return db["id"]


def seed_sync_config(pb: PBClient) -> None:
    existing = {r["collection"]: r for r in pb.list_records("sync_config")}
    for name, notion_db_id in SYNC_TARGETS.items():
        payload = {
            "collection": name,
            "notion_db_id": notion_db_id,
            "enabled": True,
            "field_map_overrides": {},
        }
        if name in existing:
            pb.update_record("sync_config", existing[name]["id"], payload)
            print(f"  [upd] sync_config[{name}]")
        else:
            pb.create_record("sync_config", payload)
            print(f"  [new] sync_config[{name}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent-page-id",
                    default=os.environ.get("NOTION_SYNC_PARENT_PAGE_ID"),
                    help="Notion page under which Sync Activity DB is created")
    args = ap.parse_args()
    if not args.parent_page_id:
        print("error: pass --parent-page-id or set NOTION_SYNC_PARENT_PAGE_ID")
        return 1

    nc = NotionClient()
    pb = PBClient()

    print("[1/3] Adding pipeline columns to existing Notion DBs:")
    for db_id in SYNC_TARGETS.values():
        add_pipeline_columns(nc, db_id)

    print("[2/3] Setting up Sync Activity DB:")
    find_or_create_activity_db(nc, args.parent_page_id)

    print("[3/3] Seeding sync_config rows in PB:")
    seed_sync_config(pb)

    print("\nDone. Next: run `scripts/reconcile_initial.py --dry-run` to preview reconcile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
