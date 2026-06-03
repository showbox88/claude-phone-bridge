#!/usr/bin/env python3
"""Phase 2 data migration: days → days + stops.

For each existing `days` row d whose `migrated_to_stop_id` is empty:
  1. Build a `stops` row carrying the moved fields:
       name, date, reserved, checkin, amount, currency, rate, amount_usd,
       location, actual_lat, actual_lng, note, trip
     plus `categories` derived from old `activity_type`.
  2. Designate a canonical day container per (trip, date_part) — the first
     d encountered for that key. All stops for that key point at that
     canonical day. Subsequent d's on the same key are duplicates whose
     data is now in stops; they get recorded to a review CSV.
  3. Write the new stops row, set d.migrated_to_stop_id = stop.id.

Idempotent: rows with `migrated_to_stop_id` already set are skipped.

Run:
    python3 scripts/migrate_days_to_stops.py --dry-run
    python3 scripts/migrate_days_to_stops.py
    python3 scripts/migrate_days_to_stops.py --apply-delete

CAUTION: `--apply-delete` physically removes duplicate day rows after
their data is in stops. Review `migrate_duplicates.csv` BEFORE using it.

See docs/superpowers/specs/2026-06-03-stops-redesign-design.md §6 Phase 2.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.backup import backup_collections
from notion_sync.pb_api import PBClient


ACTIVITY_TYPE_TO_CATEGORIES: dict[str, list[str]] = {
    "景点观光":  ["打卡"],
    "爬山/徒步": ["体验"],
    "用餐":      ["餐厅"],
    "购物":      ["购物"],
    "休息":      ["酒店"],
    "交通":      ["交通"],
    "娱乐":      ["体验"],
    "其他":      [],
    "":          [],
}


# Scalar fields that move from days to stops. (Relations: trip + location
# handled separately because they get attached to specific stops keys.)
MOVED_SCALAR_FIELDS = [
    "reserved", "checkin",
    "amount", "currency", "rate", "amount_usd",
    "actual_lat", "actual_lng", "note",
]


def stop_payload_from_day(d: dict, *, canonical_day_id: str) -> dict:
    """Build the stops record body from an existing days row.

    Empty / None scalar values are omitted so PB stores them as the
    field's natural zero. `categories` is omitted entirely when the
    activity_type mapping returns an empty list (multi-select PB field
    accepts that as "no value").
    """
    body: dict = {
        "name":     d.get("name") or "",
        "date":     d.get("date") or "",
        "trip":     d.get("trip") or "",
        "location": d.get("location") or "",
        "day":      canonical_day_id,
    }
    for f in MOVED_SCALAR_FIELDS:
        v = d.get(f)
        if v not in (None, ""):
            body[f] = v
    cats = ACTIVITY_TYPE_TO_CATEGORIES.get(d.get("activity_type") or "", [])
    if cats:
        body["categories"] = cats
    return body


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Plan only, write nothing.")
    ap.add_argument("--apply-delete", action="store_true",
                    help="After stops are created, physically delete day "
                         "rows that were 2nd+ on (trip, date). REVIEW "
                         "migrate_duplicates.csv FIRST.")
    ap.add_argument("--only-trip", default="",
                    help="Process only days rows whose trip = this PB id.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after processing this many day rows.")
    ap.add_argument("--csv-out", default="migrate_duplicates.csv",
                    help="Output CSV for duplicate-day audit.")
    ap.add_argument("--backup-root",
                    default=os.environ.get("BRIDGE_BACKUP_ROOT",
                                            ".bridge_data/backups"),
                    help="Backup destination root (snapshots all PB base "
                         "collections before any write).")
    args = ap.parse_args()

    pb = PBClient()

    if not args.dry_run:
        bak = backup_collections(pb, Path(args.backup_root))
        print(f"Backup written: {bak}")

    filt = f'trip = "{args.only_trip}"' if args.only_trip else ""
    rows = pb.list_records("days", filter=filt,
                           sort="trip,date,created", per_page=200)

    canonical_day: dict[tuple[str, str], str] = {}
    duplicates: list[dict] = []
    stops_created = 0
    skipped = 0
    processed = 0

    for d in rows:
        if args.limit and processed >= args.limit:
            break
        processed += 1

        if d.get("migrated_to_stop_id"):
            skipped += 1
            continue

        date_part = str(d.get("date") or "").split(" ")[0].split("T")[0]
        key = (d.get("trip") or "", date_part)

        canonical_day.setdefault(key, d["id"])
        is_duplicate = d["id"] != canonical_day[key]

        body = stop_payload_from_day(d, canonical_day_id=canonical_day[key])

        if args.dry_run:
            print(f"[DRY] day {d['id']} (trip={key[0] or '-'} "
                  f"date={key[1] or '-'} dup={is_duplicate}) → "
                  f"stop with categories={body.get('categories', [])}")
            stops_created += 1
            if is_duplicate:
                duplicates.append({
                    "day_id":           d["id"],
                    "trip":             key[0],
                    "date":             key[1],
                    "name":             d.get("name") or "",
                    "canonical_day_id": canonical_day[key],
                    "stop_id":          "(dry-run)",
                })
            continue

        s = pb.create_record("stops", body)
        pb.update_record("days", d["id"], {"migrated_to_stop_id": s["id"]})
        stops_created += 1

        if is_duplicate:
            duplicates.append({
                "day_id":           d["id"],
                "trip":             key[0],
                "date":             key[1],
                "name":             d.get("name") or "",
                "canonical_day_id": canonical_day[key],
                "stop_id":          s["id"],
            })

    if duplicates:
        out_path = Path(args.csv_out)
        with out_path.open("w", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=sorted(duplicates[0].keys()))
            w.writeheader()
            w.writerows(duplicates)
        print(f"Duplicate day rows written: {out_path} ({len(duplicates)} rows)")

    print(f"\nProcessed: {processed}  skipped: {skipped}  "
          f"stops_created: {stops_created}  duplicates: {len(duplicates)}")

    if args.apply_delete:
        if args.dry_run:
            print("--apply-delete ignored under --dry-run.")
        else:
            print(f"\nDeleting {len(duplicates)} duplicate day rows...")
            errs = 0
            for row in duplicates:
                try:
                    pb.delete_record("days", row["day_id"])
                except Exception as e:
                    errs += 1
                    print(f"  delete failed day_id={row['day_id']}: {e}")
            print(f"Done. errors={errs}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
