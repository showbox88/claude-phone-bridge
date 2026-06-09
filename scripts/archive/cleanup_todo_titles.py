#!/usr/bin/env python3
"""One-off: strip leading emoji from PB todo titles.

PB doesn't store page icons, so any emoji at the start of a todo title
is data clutter once Notion picks up the icon via `icon_for_todo()`.
This script removes it.

Idempotent — re-running on a title without leading emoji is a no-op.

After this runs, the next sync pass will see PbOnlyChange for every
touched todo and push the cleaned title up to Notion. The Notion icon
for that page is already set by `backfill_icons.py` (or any future
sync), reusing the stripped emoji as the icon when present.

Run:
    .venv/bin/python scripts/cleanup_todo_titles.py --dry-run
    .venv/bin/python scripts/cleanup_todo_titles.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.icons import strip_leading_emoji
from notion_sync.pb_api import PBClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()
    rows = pb.list_records("todos", sort="")
    print(f"todos total: {len(rows)}")

    changed = 0
    skipped = 0
    for r in rows:
        title = r.get("title") or ""
        clean, emoji = strip_leading_emoji(title)
        if not emoji or clean == title:
            skipped += 1
            continue
        if not clean:
            print(f"  [warn] {r['id']} title is all-emoji, leaving alone: {title!r}")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  [dry] {r['id']}: {title!r} → {clean!r} (icon={emoji!r})")
        else:
            pb.update_record("todos", r["id"], {"title": clean})
            print(f"  patched {r['id']}: {title!r} → {clean!r} (icon={emoji!r})")
        changed += 1

    print(f"\ndone. changed={changed} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
