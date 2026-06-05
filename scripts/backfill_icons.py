#!/usr/bin/env python3
"""One-time backfill: assign icons to all existing Notion Day, Trip, and
Stop pages per the policy in notion_sync.icons.

Days and Trips get unconditional uniform icons (📅 / ✈️) — existing
icons are overwritten by design (user spec: day is a container, the
semantic richness belongs at stop level via category mapping).

Stops get the category-derived icon. Currently all stops have empty
icons, so this just fills them in.

Idempotent: re-running produces the same writes (Notion accepts
identical icon writes without error).

Run:
    python scripts/backfill_icons.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.icons import icon_for
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient


def backfill(collection: str, pb: PBClient, nc: NotionClient) -> tuple[int, int]:
    """Returns (patched, skipped) counts."""
    rows = pb.list_records(collection, sort="")
    patched = 0
    skipped = 0
    for r in rows:
        nid = r.get("notion_id")
        if not nid:
            skipped += 1
            continue
        icon = icon_for(collection, r)
        if icon is None:
            skipped += 1
            continue
        try:
            nc.update_page(nid, icon=icon)
            patched += 1
        except Exception as e:
            print(f"  [warn] {collection}/{r['id']} → {nid[:8]}: {e}", file=sys.stderr)
            skipped += 1
    return patched, skipped


def main() -> int:
    pb = PBClient()
    nc = NotionClient()
    total = 0
    for c in ("days", "trips", "stops", "expenses"):
        p, s = backfill(c, pb, nc)
        print(f"{c}: patched={p} skipped={s}")
        total += p
    print(f"\nDone. Total icons set: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
