#!/usr/bin/env python3
"""One-time backfill: expenses.timezone and foods.timezone.

Inherit from parent: stop.timezone -> day.timezone -> empty.
(Both tables carry stop/day relations from earlier migrations.)

Idempotent.

Run:
    python scripts/backfill_child_timezones.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.pb_api import PBClient


def _backfill_collection(pb: PBClient, collection: str,
                         stop_tz: dict[str, str], day_tz: dict[str, str],
                         dry_run: bool) -> tuple[int, int]:
    patched = skipped = 0
    for r in pb.list_records(collection, sort=""):
        if (r.get("timezone") or "").strip():
            skipped += 1
            continue
        tz = stop_tz.get(r.get("stop") or "", "") or day_tz.get(r.get("day") or "", "")
        if not tz:
            skipped += 1
            continue
        if dry_run:
            print(f"  would patch {collection} {r['id']}: {tz}")
        else:
            pb.update_record(collection, r["id"], {"timezone": tz})
        patched += 1
    return patched, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pb = PBClient()

    stop_tz = {s["id"]: (s.get("timezone") or "").strip()
               for s in pb.list_records("stops", sort="")}
    day_tz  = {d["id"]: (d.get("timezone") or "").strip()
               for d in pb.list_records("days", sort="")}

    for col in ("expenses", "foods"):
        p, s = _backfill_collection(pb, col, stop_tz, day_tz, args.dry_run)
        print(f"{col}: patched={p} skipped={s}")
    print(f"(dry_run={args.dry_run})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
