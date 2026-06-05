#!/usr/bin/env python3
"""One-off: recover the leading emoji from Notion todo titles and store
it in the newly-added `todos.icon` field on PB.

Needed because `cleanup_todo_titles.py` ran first and stripped the emoji
from PB titles before there was anywhere to save them. Notion's side
still holds the original emoji-prefixed title (the sync hasn't pushed
the cleaned versions yet), so we can read the emoji from there.

After this runs:
- PB.icon holds the user's preserved emoji
- PB.title is clean (already done)
- Next sync pass pushes cleaned title + icon_for_todo() returns the
  preserved emoji per PB.icon

Idempotent — re-running on a todo whose icon is already set is a no-op.

Run:
    .venv/bin/python scripts/backfill_todo_icons.py --dry-run
    .venv/bin/python scripts/backfill_todo_icons.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.icons import strip_leading_emoji
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()
    nc = NotionClient()

    rows = pb.list_records("todos", sort="")
    print(f"PB todos: {len(rows)}")

    cfg = next(c for c in pb.list_records("sync_config", sort="")
               if c["collection"] == "todos")
    notion_db_id = cfg["notion_db_id"]
    notion_pages = nc.query_database(notion_db_id)
    page_by_id = {p["id"]: p for p in notion_pages}
    print(f"Notion todo pages: {len(notion_pages)}")

    set_count = 0
    skipped = 0
    for r in rows:
        if (r.get("icon") or "").strip():
            skipped += 1
            continue
        nid = r.get("notion_id") or ""
        page = page_by_id.get(nid)
        if not page:
            skipped += 1
            continue
        title_blocks = []
        for key in ("Title", "Name", "title"):
            tb = page.get("properties", {}).get(key, {}).get("title", [])
            if tb:
                title_blocks = tb
                break
        n_title = "".join(t.get("plain_text", "") for t in title_blocks)
        _, emoji = strip_leading_emoji(n_title)
        if not emoji:
            skipped += 1
            continue
        if args.dry_run:
            print(f"  [dry] {r['id']}: set icon={emoji!r} from Notion title {n_title!r}")
        else:
            pb.update_record("todos", r["id"], {"icon": emoji})
            print(f"  patched {r['id']}: icon={emoji!r}")
        set_count += 1

    print(f"\ndone. set={set_count} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
