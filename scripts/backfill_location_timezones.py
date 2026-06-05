#!/usr/bin/env python3
"""One-time backfill: fill `locations.timezone` from lat/lng for every location.

Idempotent: skips rows with non-empty timezone, and rows missing GPS.

Run:
    python scripts/backfill_location_timezones.py
    python scripts/backfill_location_timezones.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.pb_api import PBClient
from tz_resolver import gps_to_tz


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()
    rows = pb.list_records("locations", sort="")
    patched = skipped_has_tz = skipped_no_gps = failed = 0

    for r in rows:
        if (r.get("timezone") or "").strip():
            skipped_has_tz += 1
            continue
        try:
            lat = float(r.get("lat") or 0)
            lng = float(r.get("lng") or 0)
        except (TypeError, ValueError):
            skipped_no_gps += 1
            continue
        if lat == 0 and lng == 0:
            skipped_no_gps += 1
            continue
        tz = gps_to_tz(lat=lat, lng=lng)
        if not tz:
            failed += 1
            print(f"  [warn] no tz for {r['id']} ({lat},{lng})")
            continue
        if args.dry_run:
            print(f"  would patch {r['id']}: {tz}")
        else:
            pb.update_record("locations", r["id"], {"timezone": tz})
        patched += 1

    print(f"locations: patched={patched} already={skipped_has_tz} "
          f"no_gps={skipped_no_gps} failed={failed} "
          f"(dry_run={args.dry_run})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
