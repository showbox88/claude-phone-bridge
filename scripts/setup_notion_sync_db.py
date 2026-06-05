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
#
# **NOT THE SOURCE OF TRUTH** — the canonical, up-to-date sync target list is
# in `sync_config` rows in PocketBase AND in docs/data-model.md §10. This dict
# is only consulted by the one-shot bootstrap below (which has already run for
# the production workspace). The two trailing entries (stops + journal) were
# added 2026-06-03 via the stops redesign so a fresh-workspace re-run still
# bootstraps all 8 targets.
# After 2026-06-04 the per-target metadata (title_field, date_field,
# auto_sync) lives in extra columns on sync_config. This bootstrap
# script does NOT seed those — the migration
# `1779465623_extend_sync_config.js` does. See
# docs/sync-registry-design.md.
SYNC_TARGETS: dict[str, str] = {
    "trips":     "df7ea062-7b18-4c4f-98f1-bfec8258c3db",
    "days":      "13329dea-4f55-4fc8-8e64-6c1ff19353bb",
    "plans":     "c951c7a9-a8f5-4ffd-aea2-1244e437ae46",
    "todos":     "5d4e3f93-cf13-4707-97c5-59b38940baac",
    "contacts":  "e304a6c3-4771-4c69-9ffc-97a672a1ac0c",
    "locations": "257c34c1-ac50-455d-9c8a-8d810de5c1e5",
    # Added 2026-06-03 via stops redesign:
    "stops":     "15bb0429-a026-48b4-96f8-4447d5060ee3",
    "journal":   "ccc3b239-682d-47a1-a20e-e33b3c8fae44",
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
    _persist_activity_db_id(db["id"])
    return db["id"]


def _persist_env_var(key: str, value: str) -> None:
    """Append a `KEY=VALUE` line to project-root .env if not already present.

    If .env doesn't exist, prints a WARN with the line to add manually.
    If the key already exists in .env (any value), prints a WARN and
    leaves the file alone — caller updates manually rather than risk
    clobbering a hand-edited value.
    """
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        print(f"         WARN: {env_path} not found — add manually: "
              f"{key}={value}")
        return
    existing_text = env_path.read_text(encoding="utf-8")
    for line in existing_text.splitlines():
        if line.strip().startswith(f"{key}="):
            print(f"         WARN: .env already has {key} — "
                  f"please update to {value} manually")
            return
    sep = "" if existing_text.endswith("\n") else "\n"
    with env_path.open("a", encoding="utf-8") as f:
        f.write(f"{sep}{key}={value}\n")
    print(f"         [ok] appended {key} to {env_path}")


def _persist_activity_db_id(db_id: str) -> None:
    """Append NOTION_SYNC_ACTIVITY_DB_ID to .env so reconcile_initial finds it."""
    _persist_env_var("NOTION_SYNC_ACTIVITY_DB_ID", db_id)


def _persist_parent_page_id(uuid: str) -> None:
    """Append NOTION_SYNC_PARENT_PAGE_ID to .env so the provisioner can find it.

    Without this, the new "+ 新增同步表" REST flow fails with
    'NOTION_SYNC_PARENT_PAGE_ID not set' because provisioner.py reads it
    from env. The original setup historically passed --parent-page-id
    as a CLI arg without persisting, which left this variable missing
    from fresh installs / disaster-recovery rebuilds.
    """
    _persist_env_var("NOTION_SYNC_PARENT_PAGE_ID", uuid)


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

    # Persist the parent page id to .env so future REST calls (settings
    # UI → "+ 新增同步表" → provisioner.provision_notion_db) can find it
    # without the CLI arg.
    _persist_parent_page_id(args.parent_page_id)

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
