#!/usr/bin/env python3
"""One-time backfill: stops.timezone and days.timezone.

stops: location.timezone -> gps_to_tz(actual_lat, actual_lng) -> empty.
days:  first stop on that day (by reserved ASC, then created ASC) timezone.

Idempotent.

Run:
    python scripts/backfill_stop_timezones.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.pb_api import PBClient
from tz_resolver import resolve_tz


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()

    loc_tz: dict[str, str] = {}
    for loc in pb.list_records("locations", sort=""):
        loc_tz[loc["id"]] = (loc.get("timezone") or "").strip()

    stops = pb.list_records("stops", sort="")
    s_patched = s_skipped = 0
    for s in stops:
        if (s.get("timezone") or "").strip():
            s_skipped += 1
            continue
        loc_id = s.get("location") or ""
        stop_proxy = {"timezone": loc_tz.get(loc_id, "")}
        try:
            lat_raw = float(s.get("actual_lat") or 0)
            lng_raw = float(s.get("actual_lng") or 0)
        except (TypeError, ValueError):
            lat_raw = lng_raw = 0
        if lat_raw == 0 and lng_raw == 0:
            lat = lng = None
        else:
            lat, lng = lat_raw, lng_raw
        tz = resolve_tz(stop=stop_proxy, lat=lat, lng=lng)
        if not tz:
            s_skipped += 1
            continue
        if args.dry_run:
            print(f"  would patch stop {s['id']}: {tz}")
        else:
            pb.update_record("stops", s["id"], {"timezone": tz})
        s_patched += 1

    stops_after = pb.list_records("stops", sort="") if not args.dry_run else stops

    by_day: dict[str, list[dict]] = {}
    for s in stops_after:
        d = s.get("day") or ""
        if not d:
            continue
        by_day.setdefault(d, []).append(s)

    days = pb.list_records("days", sort="")
    d_patched = d_skipped = 0
    for day in days:
        if (day.get("timezone") or "").strip():
            d_skipped += 1
            continue
        ss = sorted(by_day.get(day["id"], []),
                    key=lambda x: (x.get("reserved") or "", x.get("created") or ""))
        if not ss:
            d_skipped += 1
            continue
        tz = (ss[0].get("timezone") or "").strip()
        if not tz:
            d_skipped += 1
            continue
        if args.dry_run:
            print(f"  would patch day {day['id']}: {tz}")
        else:
            pb.update_record("days", day["id"], {"timezone": tz})
        d_patched += 1

    print(f"stops: patched={s_patched} skipped={s_skipped}")
    print(f"days:  patched={d_patched} skipped={d_skipped}")
    print(f"(dry_run={args.dry_run})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
