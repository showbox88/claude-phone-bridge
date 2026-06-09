#!/usr/bin/env python3
"""Migrate `transactions` rows → `expenses` rows.

Behavior:
  - Idempotent: a transaction whose `confirmation` already exists in expenses
    (via the UNIQUE index on confirmation) is skipped; transactions with empty
    confirmation are skipped if an expense with same (date, description, amount)
    already exists.
  - 代付 detection: description containing "代付" → expense_category overridden
    to "代付" regardless of source category.
  - Auto day backfill: for each transaction, find days.date == tx.date. If
    multiple, prefer one whose trip date_start ≤ tx.date ≤ trip date_end. If
    none exists, create a new day with name == tx.date (YYYY-MM-DD), date,
    trip = matching trip (if any) else empty.
  - Auto trip backfill: expense.trip = day.trip (may be empty).
  - Refund sign: type == '退款' AND amount > 0 → amount becomes negative.
  - currency = 'USD' for all (legacy transactions were USD).
  - rate = 0 (empty), amount_usd = amount.

Run:
    .venv/bin/python scripts/migrate_transactions_to_expenses.py --dry-run
    .venv/bin/python scripts/migrate_transactions_to_expenses.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.pb_api import PBClient


def parse_pb_date(s: str) -> str:
    """PB stores 'YYYY-MM-DD HH:MM:SS.SSSZ'. Return 'YYYY-MM-DD' part."""
    if not s:
        return ""
    return s[:10]


def find_or_create_day(pb: PBClient, *, tx_date: str, trips: list[dict], dry_run: bool) -> dict | None:
    """Return a day row matching tx_date (existing or newly created).

    Returns None in dry_run when no existing day matches.
    """
    date_only = parse_pb_date(tx_date)
    if not date_only:
        return None

    existing = pb.list_records(
        "days",
        filter=f'date >= "{date_only} 00:00:00" && date < "{date_only} 23:59:59"',
        per_page=10,
    )
    if existing:
        for d in existing:
            t = d.get("trip")
            if not t:
                continue
            trip = next((x for x in trips if x["id"] == t), None)
            if not trip:
                continue
            ds = parse_pb_date(trip.get("date_start", ""))
            de = parse_pb_date(trip.get("date_end", ""))
            if ds <= date_only <= de:
                return d
        return existing[0]

    matching_trip = ""
    for trip in trips:
        ds = parse_pb_date(trip.get("date_start", ""))
        de = parse_pb_date(trip.get("date_end", ""))
        if ds and de and ds <= date_only <= de:
            matching_trip = trip["id"]
            break

    payload = {"name": date_only, "date": date_only, "trip": matching_trip}
    if dry_run:
        print(f"  [dry-run] would create day: {payload}")
        return None
    new_day = pb.create_record("days", payload)
    print(f"  created day: {new_day['id']} ({date_only}) trip={matching_trip or '(none)'}")
    return new_day


def expense_payload_from_tx(tx: dict, *, day_id: str, trip_id: str) -> dict:
    desc = tx.get("description") or ""
    category = tx.get("category") or "其他"
    if "代付" in desc:
        category = "代付"

    amount = tx.get("amount") or 0
    if tx.get("type") == "退款" and amount > 0:
        amount = -amount

    return {
        "description":      desc,
        "amount":           amount,
        "currency":         "USD",
        "rate":             0,
        "amount_usd":       amount,
        "date":             tx.get("date") or "",
        "type":             tx.get("type") or "支出",
        "expense_category": category,
        "card":             tx.get("card") or "",
        "confirmation":     tx.get("confirmation") or "",
        "source":           tx.get("source") or "手动",
        "stop":             "",
        "day":              day_id,
        "trip":             trip_id,
    }


def already_migrated(pb: PBClient, tx: dict) -> bool:
    conf = (tx.get("confirmation") or "").strip()
    if conf:
        existing = pb.list_records("expenses", filter=f'confirmation = "{conf}"', per_page=1)
        if existing:
            return True
    date = (tx.get("date") or "")[:10]
    desc = (tx.get("description") or "").replace('"', '\\"')
    amount = tx.get("amount") or 0
    if not date or not desc:
        return False
    filt = f'date >= "{date} 00:00:00" && date < "{date} 23:59:59" && description = "{desc}" && amount = {amount}'
    existing = pb.list_records("expenses", filter=filt, per_page=1)
    return bool(existing)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()

    txs = pb.list_records("transactions", per_page=500)
    trips = pb.list_records("trips", per_page=500)
    print(f"transactions to migrate: {len(txs)}")
    print(f"trips available for matching: {len(trips)}")

    created = 0
    skipped = 0
    for tx in txs:
        desc = tx.get("description") or "(no desc)"
        if already_migrated(pb, tx):
            print(f"  skip (already migrated): {desc[:60]}")
            skipped += 1
            continue
        day = find_or_create_day(pb, tx_date=tx.get("date") or "", trips=trips, dry_run=args.dry_run)
        day_id = day["id"] if day else ""
        trip_id = (day.get("trip") if day else "") or ""
        payload = expense_payload_from_tx(tx, day_id=day_id, trip_id=trip_id)
        if args.dry_run:
            print(f"  [dry-run] would create expense: {payload}")
        else:
            pb.create_record("expenses", payload)
            print(f"  created expense: {desc[:60]} → day={day_id} trip={trip_id or '(none)'}")
        created += 1

    print(f"\ndone. created={created} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
