#!/usr/bin/env python3
"""One-off: backfill Notion relation columns for every synced collection.

Goes through every enabled sync_config row, builds the PB→Notion lookup,
and PATCHes each Notion page's relation properties using the new
relation-translation logic in notion_sync.transform.

Idempotent — re-running on a page whose relation columns are already
correct is a harmless re-PATCH. Use after the relation-sync code change
to retroactively fill columns that were left empty by the older runner.

Run:
    .venv/bin/python scripts/backfill_relations.py [--only expenses] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.codec import snake_to_title
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.transform import (
    build_relation_lookup,
    collection_field_types,
    relation_target_collections,
)


def backfill_collection(collection: str, notion_db_id: str,
                        pb: PBClient, nc: NotionClient,
                        relation_lookup: dict[str, dict[str, str]],
                        dry_run: bool) -> tuple[int, int, int]:
    field_types = collection_field_types(pb, collection)
    relation_fields = {n for n, s in field_types.items() if s["type"] == "relation"}
    if not relation_fields:
        return 0, 0, 0

    targets = relation_target_collections(pb, collection)
    notion_db = nc.retrieve_database(notion_db_id)
    notion_schema = notion_db.get("properties", {})

    notion_name_by_pb: dict[str, str] = {}
    for pb_name in relation_fields:
        candidate = snake_to_title(pb_name)
        if candidate in notion_schema and notion_schema[candidate].get("type") == "relation":
            notion_name_by_pb[pb_name] = candidate

    if not notion_name_by_pb:
        return 0, 0, 0

    rows = pb.list_records(collection, sort="")
    patched = 0
    skipped = 0
    for r in rows:
        nid = r.get("notion_id") or ""
        if not nid:
            skipped += 1
            continue
        props: dict = {}
        for pb_name, notion_name in notion_name_by_pb.items():
            value = r.get(pb_name)
            target_col = targets.get(pb_name)
            if not target_col:
                continue
            target_map = relation_lookup.get(target_col, {})
            if isinstance(value, str):
                pb_ids = [value] if value else []
            elif isinstance(value, list):
                pb_ids = [v for v in value if v]
            else:
                pb_ids = []
            notion_refs = [{"id": target_map[pid]} for pid in pb_ids if pid in target_map]
            props[notion_name] = {"relation": notion_refs}
        if not props:
            skipped += 1
            continue
        if dry_run:
            summary = {k: len(v["relation"]) for k, v in props.items()}
            print(f"  [dry] {r['id']} → patch {summary}")
        else:
            try:
                nc.update_page(nid, properties=props)
            except Exception as e:
                print(f"  [warn] {collection}/{r['id']} → {nid[:8]}: {e}", file=sys.stderr)
                continue
        patched += 1
    return patched, skipped, len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()
    nc = NotionClient()

    all_targets = pb.list_records("sync_config", filter="enabled=true", sort="")
    targets = all_targets
    if args.only:
        targets = [t for t in all_targets if t["collection"] == args.only]
    target_names = [t["collection"] for t in targets]
    print(f"sync targets in scope: {target_names}")

    relation_lookup = build_relation_lookup(
        pb, [t["collection"] for t in all_targets]
    )

    for t in targets:
        c = t["collection"]
        db = t["notion_db_id"]
        patched, skipped, total = backfill_collection(
            c, db, pb, nc, relation_lookup, args.dry_run
        )
        print(f"{c}: patched={patched} skipped={skipped} total={total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
