#!/usr/bin/env python3
"""Migrate money fields on `stops` rows → new `expenses` rows.

For each stop where amount > 0, create one expense linked back to the stop.
Idempotent: a stop whose id already appears as an expense.stop is skipped.

  - description = stops.name + (if note non-empty) " · " + stops.note
                  (truncated to 500 chars)
  - amount, currency, rate, amount_usd → copied as-is
  - date → stops.date
  - type → "支出" (no legacy refund-on-stop concept)
  - expense_category → inferred from stops.categories (see CATEGORY_MAP)
  - source → "手动"
  - stop = stops.id, day = stops.day, trip = stops.trip
  - card / confirmation → empty

Run:
    .venv/bin/python scripts/migrate_stops_money_to_expenses.py --dry-run
    .venv/bin/python scripts/migrate_stops_money_to_expenses.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.pb_api import PBClient


CATEGORY_MAP = [
    ("餐厅", "餐饮"),
    ("酒店", "住宿"),
    ("交通", "交通"),
    ("购物", "购物/日用"),
    ("体验", "娱乐"),
    ("打卡", "门票"),
]


def infer_category(categories: list[str]) -> str:
    if not categories:
        return "其他"
    for tag, mapped in CATEGORY_MAP:
        if tag in categories:
            return mapped
    return "其他"


def build_description(stop: dict) -> str:
    name = (stop.get("name") or "").strip()
    note = (stop.get("note") or "").strip()
    if note:
        desc = f"{name} · {note}"
    else:
        desc = name
    return desc[:500]


def already_migrated(pb: PBClient, stop_id: str) -> bool:
    existing = pb.list_records("expenses", filter=f'stop = "{stop_id}"', per_page=1)
    return bool(existing)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()
    stops = pb.list_records("stops", per_page=1000)
    print(f"total stops: {len(stops)}")

    money_stops = [s for s in stops if (s.get("amount") or 0) > 0]
    print(f"stops with amount > 0: {len(money_stops)}")

    created = 0
    skipped = 0
    for s in money_stops:
        if already_migrated(pb, s["id"]):
            print(f"  skip (already migrated): {s.get('name')}")
            skipped += 1
            continue
        payload = {
            "description":      build_description(s),
            "amount":           s.get("amount") or 0,
            "currency":         s.get("currency") or "USD",
            "rate":             s.get("rate") or 0,
            "amount_usd":       s.get("amount_usd") or s.get("amount") or 0,
            "date":             s.get("date") or "",
            "type":             "支出",
            "expense_category": infer_category(s.get("categories") or []),
            "card":             "",
            "confirmation":     "",
            "source":           "手动",
            "stop":             s["id"],
            "day":              s.get("day") or "",
            "trip":             s.get("trip") or "",
        }
        if args.dry_run:
            print(f"  [dry-run] would create expense: {payload}")
        else:
            pb.create_record("expenses", payload)
            print(f"  created expense for stop {s['id']} ({s.get('name')})")
        created += 1

    print(f"\ndone. created={created} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
