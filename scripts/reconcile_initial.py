#!/usr/bin/env python3
"""Initial Notion ↔ PB data alignment (PR1 one-shot).

For each enabled sync_config row:
  1. Skip already-linked rows.
  2. Fuzzy-match by title + date.
       score >= 0.95  → auto-link (write both IDs back)
       score >= 0.60  → write Possible duplicate to Sync Activity
       otherwise      → unmatched
  3. For unmatched residuals: create matching pages/records on the opposite
     side and write IDs back.

Backs up PB to .bridge_data/backups/<ts>/ before any write.

Run:
    python3 scripts/reconcile_initial.py            # full
    python3 scripts/reconcile_initial.py --only trips
    python3 scripts/reconcile_initial.py --dry-run  # log, write nothing
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.activity import write_possible_duplicate
from notion_sync.backup import backup_collections
from notion_sync.codec import (
    notion_property_to_pb_field,
    pb_field_to_notion_property,
    snake_to_title,
    title_to_snake,
)
from notion_sync.matching import best_match
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient


TITLE_FIELD_BY_COLLECTION = {
    "trips": "title", "plans": "title", "todos": "title",
    "days":  "name",  "contacts": "name", "locations": "name",
}
DATE_FIELD_BY_COLLECTION = {
    "trips": "date_start", "days": "date",
    "todos": "due_date",   "plans": "target_date",
    "contacts": "",        "locations": "",
}


def collection_field_types(pb: PBClient, name: str) -> dict[str, dict]:
    for c in pb.list_collections():
        if c["name"] == name:
            return {
                f["name"]: {"type": f["type"], "maxSelect": f.get("maxSelect", 1)}
                for f in c.get("fields", [])
            }
    raise RuntimeError(f"collection not found: {name}")


def now_iso_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_iso_datetime() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def notion_page_to_pb_dict(page: dict, field_types: dict[str, dict],
                           overrides: dict[str, str]) -> dict:
    out: dict = {}
    for prop_name, prop_val in page.get("properties", {}).items():
        pb_name = overrides.get(prop_name, title_to_snake(prop_name))
        if pb_name not in field_types:
            continue
        spec = field_types[pb_name]
        out[pb_name] = notion_property_to_pb_field(
            prop_val, pb_type=spec["type"], max_select=spec.get("maxSelect", 1)
        )
    return out


def pb_record_to_notion_props(record: dict, field_types: dict[str, dict],
                              overrides_inv: dict[str, str],
                              title_field: str) -> dict:
    SKIP = {"id", "created", "updated", "collectionId", "collectionName",
            "expand", "notion_id", "notion_last_edited", "last_synced_at"}
    notion_title_prop = snake_to_title(title_field)
    props: dict = {}
    for pb_name, value in record.items():
        if pb_name in SKIP:
            continue
        if pb_name not in field_types:
            continue
        if pb_name == title_field:
            continue
        spec = field_types[pb_name]
        notion_name = overrides_inv.get(pb_name, snake_to_title(pb_name))
        props[notion_name] = pb_field_to_notion_property(
            value, pb_type=spec["type"], max_select=spec.get("maxSelect", 1)
        )
    title_val = record.get(title_field, "") or ""
    props[notion_title_prop] = {"title": [{"type": "text",
                                            "text": {"content": str(title_val)[:200]}}]}
    return props


def _pb_id_in_notion_page(p: dict) -> str:
    prop = p.get("properties", {}).get("pb_id", {})
    return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))


def reconcile_one(collection: str, notion_db_id: str,
                  overrides: dict[str, str],
                  pb: PBClient, nc: NotionClient,
                  dry_run: bool) -> dict:
    print(f"\n=== {collection} ===")
    overrides_inv = {v: k for k, v in overrides.items()}
    field_types = collection_field_types(pb, collection)
    title_field = TITLE_FIELD_BY_COLLECTION.get(collection, "title")
    date_field = DATE_FIELD_BY_COLLECTION.get(collection, "")

    pb_rows = pb.list_records(collection)
    notion_rows = nc.query_database(notion_db_id)
    print(f"  PB: {len(pb_rows)} rows  |  Notion: {len(notion_rows)} pages")

    pb_id_set = {r["id"] for r in pb_rows}
    linked = 0
    notion_unmatched: list[dict] = []
    for p in notion_rows:
        pid = _pb_id_in_notion_page(p)
        if pid and pid in pb_id_set:
            linked += 1
        else:
            notion_unmatched.append(p)
    pb_unmatched = [r for r in pb_rows if not r.get("notion_id")]
    print(f"  already linked: {linked}")

    pb_candidates = [
        {"_pb": r,
         "title": r.get(title_field, "") or "",
         "date":  r.get(date_field, "") if date_field else ""}
        for r in pb_unmatched
    ]
    used_pb_ids: set[str] = set()
    used_notion_ids: set[str] = set()
    auto_linked = 0
    queued = 0

    for npage in notion_unmatched:
        npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
        target = {
            "title": npage_dict.get(title_field, "") or "",
            "date":  npage_dict.get(date_field, "") if date_field else "",
        }
        free = [c for c in pb_candidates if c["_pb"]["id"] not in used_pb_ids]
        m = best_match(target, free, title_key="title", date_key="date",
                        min_score=0.0)
        if m is None:
            continue
        if m.score >= 0.95:
            if dry_run:
                print(f"  [dry] auto-link Notion={npage['id'][:8]} ↔ "
                       f"PB={m.record['_pb']['id'][:8]} ({m.score:.2f})")
            else:
                pb.update_record(collection, m.record["_pb"]["id"], {
                    "notion_id": npage["id"],
                    "notion_last_edited": npage.get("last_edited_time"),
                    "last_synced_at": now_iso_datetime(),
                })
                nc.update_page(npage["id"], properties={
                    "pb_id": {"rich_text": [{"type": "text",
                                              "text": {"content": m.record["_pb"]["id"]}}]},
                    "last_synced_at": {"date": {"start": now_iso_date()}},
                })
            used_pb_ids.add(m.record["_pb"]["id"])
            used_notion_ids.add(npage["id"])
            auto_linked += 1
        elif m.score >= 0.60:
            if not dry_run:
                write_possible_duplicate(
                    nc,
                    collection=collection,
                    summary=f"{target['title'][:40]} ≈ {m.record['title'][:40]} "
                             f"(score {m.score:.2f})",
                    pb_id=m.record["_pb"]["id"],
                    notion_id=npage["id"],
                    pb_snapshot=m.record["_pb"],
                    notion_snapshot=npage_dict,
                    score=m.score,
                )
            else:
                print(f"  [dry] queue Notion={npage['id'][:8]} ↔ "
                       f"PB={m.record['_pb']['id'][:8]} ({m.score:.2f})")
            queued += 1

    pb_only = [r for r in pb_unmatched if r["id"] not in used_pb_ids]
    pb_only_created = 0
    for r in pb_only:
        props = pb_record_to_notion_props(r, field_types, overrides_inv, title_field)
        props["pb_id"] = {"rich_text": [{"type": "text", "text": {"content": r["id"]}}]}
        props["last_synced_at"] = {"date": {"start": now_iso_date()}}
        if dry_run:
            print(f"  [dry] create Notion for PB={r['id'][:8]} "
                   f"title={r.get(title_field, '')[:30]!r}")
        else:
            page = nc.create_page(notion_db_id, props)
            pb.update_record(collection, r["id"], {
                "notion_id": page["id"],
                "notion_last_edited": page.get("last_edited_time"),
                "last_synced_at": now_iso_datetime(),
            })
        pb_only_created += 1

    notion_only = [p for p in notion_unmatched if p["id"] not in used_notion_ids]
    notion_only_created = 0
    for npage in notion_only:
        npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
        if dry_run:
            print(f"  [dry] create PB for Notion={npage['id'][:8]} "
                   f"title={npage_dict.get(title_field, '')[:30]!r}")
        else:
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
        notion_only_created += 1

    summary = (f"linked={linked} auto-linked={auto_linked} "
                f"queued={queued} pb→notion={pb_only_created} "
                f"notion→pb={notion_only_created}")
    print(f"  summary: {summary}")
    return {"summary": summary}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="single collection (e.g. trips)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()
    nc = NotionClient()

    if not os.environ.get("NOTION_SYNC_ACTIVITY_DB_ID"):
        print("error: NOTION_SYNC_ACTIVITY_DB_ID not set — run "
              "scripts/setup_notion_sync_db.py first")
        return 1

    if not args.dry_run:
        backup_root = Path(os.environ.get("BRIDGE_DATA_DIR", ".bridge_data")) / "backups"
        out = backup_collections(pb, backup_root)
        print(f"PB backup written: {out}")

    targets = pb.list_records("sync_config", filter="enabled=true")
    if args.only:
        targets = [t for t in targets if t["collection"] == args.only]
        if not targets:
            print(f"error: no enabled sync_config row for collection={args.only!r}")
            return 1

    for t in targets:
        try:
            result = reconcile_one(
                collection=t["collection"],
                notion_db_id=t["notion_db_id"],
                overrides=t.get("field_map_overrides") or {},
                pb=pb, nc=nc,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                pb.update_record("sync_config", t["id"], {
                    "last_synced_at": now_iso_datetime(),
                    "last_sync_summary": "reconcile_initial: " + result["summary"],
                })
        except Exception as e:
            print(f"  !! reconcile FAILED for {t['collection']}: {e}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
