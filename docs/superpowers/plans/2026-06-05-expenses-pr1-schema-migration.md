# Expenses Redesign PR1: Schema + Data Migration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate PocketBase from `transactions` + money-bearing `stops` to a `transactions`→`expenses` rename where `expenses` is a child of `stops`/`days`/`trips`. Includes data migration of 11 transaction rows + N money-bearing stop rows, with 代付 detection and auto-day backfill.

**Architecture:** Two-phase deploy. Phase A applies additive schema migrations (days.trip → optional, create expenses). Phase B runs Python data migration scripts. Phase C applies subtractive migrations (drop transactions, drop money fields from stops) gated by safety checks. Every phase preceded by a PB snapshot backup.

**Tech Stack:** PocketBase migration JS (auto-applied by deploy script via `cp -u`), Python 3 migration scripts (`notion_sync.pb_api.PBClient`), pytest for verification.

**Spec:** [docs/superpowers/specs/2026-06-05-expenses-redesign-design.md](../specs/2026-06-05-expenses-redesign-design.md)

---

## File Structure

**New files:**
- `pocketbase/pb_migrations/1779465625_days_trip_optional.js` — relax days.trip required→optional
- `pocketbase/pb_migrations/1779465626_create_expenses.js` — create expenses collection w/ pipeline fields
- `pocketbase/pb_migrations/1779465627_drop_transactions.js` — drop old transactions collection (safety-gated)
- `pocketbase/pb_migrations/1779465628_drop_stops_money_fields.js` — drop amount/currency/rate/amount_usd from stops (safety-gated)
- `scripts/migrate_transactions_to_expenses.py` — copy 11 rows + 代付 detection + day backfill
- `scripts/migrate_stops_money_to_expenses.py` — create expenses from money-bearing stops + category inference
- `tests/test_expenses_migration.py` — verify counts + field mappings + 代付 + day backfill + test-data preservation

**Modified files:**
- `docs/data-model.md` — add expenses section, update stops/days sections (defer to PR2 sync wiring details)
- `CHANGELOG.md` — log PR1
- `CLAUDE.md` — short note pointing at this spec

**No frontend / no MCP / no sync registry changes in PR1** — all that goes to PR2.

---

## Phase A: Additive schema migrations (one deploy)

### Task 1: Snapshot PB backup BEFORE any change

**Files:**
- Run: `.venv/bin/python -c "..."` against prod PB

- [ ] **Step 1: SSH to dashboard-server and snapshot**

Run:
```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -c "
from pathlib import Path
from notion_sync.pb_api import PBClient
from notion_sync.backup import backup_collections
path = backup_collections(PBClient(), Path('.bridge_data/backups'))
print('SNAPSHOT:', path)
"
```

Expected: prints something like `SNAPSHOT: .bridge_data/backups/20260605T....`
Record the path. If anything goes sideways later, that's the rollback source.

- [ ] **Step 2: Verify transactions + stops counts in the snapshot**

Run on dashboard-server:
```bash
ls -lh .bridge_data/backups/<ts>/
.venv/bin/python -c "
import json
from pathlib import Path
tx = json.loads(Path('.bridge_data/backups/<ts>/transactions.json').read_text())
st = json.loads(Path('.bridge_data/backups/<ts>/stops.json').read_text())
print('transactions:', len(tx))
print('stops with amount>0:', sum(1 for r in st if (r.get('amount') or 0) > 0))
"
```

Expected: `transactions: 11`, `stops with amount>0: N` (record N — should be ≥ 2 from today's 坐火车+冰淇淋).

---

### Task 2: Migration 1779465625 — days.trip optional

**Files:**
- Create: `pocketbase/pb_migrations/1779465625_days_trip_optional.js`

- [ ] **Step 1: Write the migration**

Create `pocketbase/pb_migrations/1779465625_days_trip_optional.js`:

```javascript
/// <reference path="../pb_data/types.d.ts" />
//
// Relax days.trip from required → optional. Needed so the expenses data
// migration can auto-create day containers for old transactions that have
// no trip (日常消费).
//
// See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md.
//
migrate((app) => {
  const c = app.findCollectionByNameOrId("days");
  const f = c.fields.getByName("trip");
  if (!f) throw new Error("days.trip field missing");
  f.required = false;
  app.save(c);
}, (app) => {
  const c = app.findCollectionByNameOrId("days");
  const f = c.fields.getByName("trip");
  if (f) {
    f.required = true;
    app.save(c);
  }
});
```

- [ ] **Step 2: Commit**

```bash
git add pocketbase/pb_migrations/1779465625_days_trip_optional.js
git commit -m "feat(pb): days.trip relax to optional (expenses redesign prep)"
```

---

### Task 3: Migration 1779465626 — create expenses collection

**Files:**
- Create: `pocketbase/pb_migrations/1779465626_create_expenses.js`

- [ ] **Step 1: Write the migration**

Create `pocketbase/pb_migrations/1779465626_create_expenses.js`:

```javascript
/// <reference path="../pb_data/types.d.ts" />
//
// Create `expenses` — the new child-of-stops money table. Replaces the
// `transactions` collection. transactions is left alone here; a separate
// Python script copies rows over, then a later migration drops transactions.
//
// See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md.
//
migrate((app) => {
  const stops = app.findCollectionByNameOrId("stops");
  const days  = app.findCollectionByNameOrId("days");
  const trips = app.findCollectionByNameOrId("trips");

  const collection = new Collection({
    name: "expenses",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "description",      type: "text", required: true, max: 500 },
      { name: "amount",           type: "number" },
      {
        name: "currency",
        type: "select",
        maxSelect: 1,
        values: ["USD", "JPY", "EUR", "CNY", "其他"],
      },
      { name: "rate",             type: "number" },
      { name: "amount_usd",       type: "number" },
      { name: "date",             type: "date" },
      {
        name: "type",
        type: "select",
        maxSelect: 1,
        values: ["支出", "退款"],
      },
      {
        name: "expense_category",
        type: "select",
        maxSelect: 1,
        values: [
          "旅行", "订阅服务", "娱乐", "交通", "购物/日用",
          "餐饮", "门票", "住宿", "代付", "其他",
        ],
      },
      {
        name: "card",
        type: "select",
        maxSelect: 1,
        values: ["Chase Sapphire Preferred (7675)"],
      },
      { name: "confirmation",     type: "text" },
      {
        name: "source",
        type: "select",
        maxSelect: 1,
        values: ["手动", "Gmail", "Agent"],
      },

      // relations — PB side only this round (sync ignores relations)
      { name: "stop", type: "relation", collectionId: stops.id, maxSelect: 1, cascadeDelete: false },
      { name: "day",  type: "relation", collectionId: days.id,  maxSelect: 1, cascadeDelete: false },
      { name: "trip", type: "relation", collectionId: trips.id, maxSelect: 1, cascadeDelete: false },

      // sync pipeline (for PR2; harmless if PR2 not shipped)
      { name: "notion_id",          type: "text", max: 100 },
      { name: "notion_last_edited", type: "date" },
      { name: "last_synced_at",     type: "date" },

      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_expenses_date     ON expenses (date)",
      "CREATE INDEX idx_expenses_category ON expenses (expense_category)",
      "CREATE INDEX idx_expenses_stop     ON expenses (stop)",
      "CREATE INDEX idx_expenses_day      ON expenses (day)",
      "CREATE INDEX idx_expenses_trip     ON expenses (trip)",
      "CREATE UNIQUE INDEX idx_expenses_confirmation ON expenses (confirmation) WHERE confirmation != ''",
      "CREATE UNIQUE INDEX idx_expenses_notion_id    ON expenses (notion_id)    WHERE notion_id != ''",
    ],
  });
  app.save(collection);
}, (app) => {
  const c = app.findCollectionByNameOrId("expenses");
  app.delete(c);
});
```

- [ ] **Step 2: Commit**

```bash
git add pocketbase/pb_migrations/1779465626_create_expenses.js
git commit -m "feat(pb): create expenses collection (transactions successor)"
```

---

### Task 4: Deploy Phase A migrations

- [ ] **Step 1: Deploy**

Run from project root:
```powershell
deploy
```

Expected:
- Tar + upload + venv recreated if needed
- `sudo systemctl restart phone-bridge`
- Health check passes
- PB restart applies migrations 1779465625 + 1779465626

- [ ] **Step 2: Verify both migrations applied**

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
import requests
r = requests.get(pb.base + '/api/collections/expenses', headers={'Authorization': 'Bearer ' + pb.token})
print('expenses collection:', r.status_code)
r = requests.get(pb.base + '/api/collections/days', headers={'Authorization': 'Bearer ' + pb.token})
j = r.json()
trip_field = next(f for f in j['fields'] if f['name'] == 'trip')
print('days.trip required =', trip_field.get('required', False))
"
```

Expected:
- `expenses collection: 200`
- `days.trip required = False`

If either fails — STOP. Investigate journalctl for migration errors. Do not proceed to Phase B.

---

## Phase B: Python data migration

### Task 5: Migration script — transactions → expenses

**Files:**
- Create: `scripts/migrate_transactions_to_expenses.py`

- [ ] **Step 1: Write the script**

Create `scripts/migrate_transactions_to_expenses.py`:

```python
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
    trip = matching trip (if any) else null.
  - Auto trip backfill: expense.trip = day.trip (may be null).
  - Refund sign: type == '退款' AND amount > 0 → amount becomes negative.
  - currency = 'USD' for all (legacy transactions were USD).
  - rate = 0 (empty), amount_usd = amount.

Run:
    python3 scripts/migrate_transactions_to_expenses.py --dry-run
    python3 scripts/migrate_transactions_to_expenses.py

The script does NOT delete the transactions table. That's a later JS migration
gated on the count check.
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

    Returns None in dry_run when no existing day matches; the caller logs
    what would be created.
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

    matching_trip = None
    for trip in trips:
        ds = parse_pb_date(trip.get("date_start", ""))
        de = parse_pb_date(trip.get("date_end", ""))
        if ds and de and ds <= date_only <= de:
            matching_trip = trip["id"]
            break

    payload = {"name": date_only, "date": date_only, "trip": matching_trip or ""}
    if dry_run:
        print(f"  [dry-run] would create day: {payload}")
        return None
    new_day = pb.create_record("days", payload)
    print(f"  created day: {new_day['id']} ({date_only}) trip={matching_trip or '(none)'}")
    return new_day


def expense_payload_from_tx(tx: dict, *, day_id: str | None, trip_id: str | None) -> dict:
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
        "day":              day_id or "",
        "trip":             trip_id or "",
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
        day_id = day["id"] if day else None
        trip_id = (day.get("trip") if day else None) or None
        payload = expense_payload_from_tx(tx, day_id=day_id, trip_id=trip_id)
        if args.dry_run:
            print(f"  [dry-run] would create expense: {payload}")
        else:
            pb.create_record("expenses", payload)
            print(f"  created expense: {desc[:60]} → day={day_id} trip={trip_id}")
        created += 1

    print(f"\ndone. created={created} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Commit**

```bash
git add scripts/migrate_transactions_to_expenses.py
git commit -m "feat: transactions→expenses migration script"
```

---

### Task 6: Migration script — stops.amount → expenses

**Files:**
- Create: `scripts/migrate_stops_money_to_expenses.py`

- [ ] **Step 1: Write the script**

Create `scripts/migrate_stops_money_to_expenses.py`:

```python
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
    python3 scripts/migrate_stops_money_to_expenses.py --dry-run
    python3 scripts/migrate_stops_money_to_expenses.py
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
```

- [ ] **Step 2: Commit**

```bash
git add scripts/migrate_stops_money_to_expenses.py
git commit -m "feat: stops money→expenses migration script"
```

---

### Task 7: Verification test

**Files:**
- Create: `tests/test_expenses_migration.py`

- [ ] **Step 1: Write the test**

Create `tests/test_expenses_migration.py`:

```python
"""Verification checks run after Phase B data migration.

NOT pure unit tests — these hit the live PB. Run on dashboard-server:
    .venv/bin/python -m pytest tests/test_expenses_migration.py -v

If any assertion fails, do NOT proceed to Phase C.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.pb_api import PBClient


@pytest.fixture(scope="module")
def pb():
    return PBClient()


@pytest.fixture(scope="module")
def transactions(pb):
    return pb.list_records("transactions", per_page=500)


@pytest.fixture(scope="module")
def expenses(pb):
    return pb.list_records("expenses", per_page=1000)


@pytest.fixture(scope="module")
def stops(pb):
    return pb.list_records("stops", per_page=1000)


def test_all_transactions_migrated(transactions, expenses):
    missing = []
    for tx in transactions:
        conf = (tx.get("confirmation") or "").strip()
        date = (tx.get("date") or "")[:10]
        if conf:
            match = [e for e in expenses if (e.get("confirmation") or "").strip() == conf]
        else:
            desc = tx.get("description") or ""
            amount = tx.get("amount") or 0
            match = [
                e for e in expenses
                if (e.get("description") or "") == desc
                and (e.get("amount") or 0) == amount
                and (e.get("date") or "")[:10] == date
            ]
        if not match:
            missing.append(tx.get("description") or tx["id"])
    assert not missing, f"transactions not migrated: {missing}"


def test_daifu_recategorized(expenses):
    daifu = [e for e in expenses if "代付" in (e.get("description") or "")]
    assert daifu, "should have ≥1 expense with 代付 in description"
    for e in daifu:
        assert e.get("expense_category") == "代付", (
            f"expense {e['id']} has 代付 in desc but category = {e.get('expense_category')}"
        )


def test_all_expenses_have_day(expenses):
    for e in expenses:
        if not e.get("stop"):
            assert e.get("day"), f"expense {e['id']} ({e.get('description')}) has no day"


def test_stops_money_migrated(stops, expenses):
    money_stops = [s for s in stops if (s.get("amount") or 0) > 0]
    for s in money_stops:
        match = [e for e in expenses if e.get("stop") == s["id"]]
        assert match, f"stop {s['id']} ({s.get('name')}) amount={s.get('amount')} has no expense"


def test_test_data_preserved(stops):
    test_ids = ["jz7w7xmn6qtelz0", "5c2u8bv4sb1os7v", "ooib2vje4194rju", "6q1kvi3qk2f1bqw"]
    for tid in test_ids:
        s = next((x for x in stops if x["id"] == tid), None)
        if s is None:
            pytest.skip(f"test stop {tid} not present (maybe already cleaned up)")
        assert "测试数据" in (s.get("note") or ""), (
            f"test stop {tid} note missing 测试数据 marker: {s.get('note')}"
        )


def test_expense_trip_matches_day_trip(expenses, pb):
    days_by_id = {d["id"]: d for d in pb.list_records("days", per_page=1000)}
    for e in expenses:
        day_id = e.get("day")
        if not day_id:
            continue
        d = days_by_id.get(day_id)
        if d is None:
            continue
        day_trip = d.get("trip") or ""
        exp_trip = e.get("trip") or ""
        assert exp_trip == day_trip, (
            f"expense {e['id']} trip={exp_trip} ≠ its day's trip={day_trip}"
        )
```

- [ ] **Step 2: Commit**

```bash
git add tests/test_expenses_migration.py
git commit -m "test: expenses migration verification checks"
```

---

### Task 8: Run Phase B on prod

- [ ] **Step 1: Deploy scripts to dashboard-server**

```powershell
deploy
```

- [ ] **Step 2: Dry-run transactions migration**

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python scripts/migrate_transactions_to_expenses.py --dry-run
```

Expected: prints 11 expense payloads. Review each:
- 4 with "代付 Monica" in description show `'expense_category': '代付'`
- New days for old dates look sensible
- transactions whose date falls inside a trip's range pick up the trip

If output looks wrong — abort, fix script, redeploy, retry.

- [ ] **Step 3: Real run transactions migration**

```bash
.venv/bin/python scripts/migrate_transactions_to_expenses.py
```

Expected: `done. created=11 skipped=0`. Re-running gives `created=0 skipped=11`.

- [ ] **Step 4: Dry-run stops money migration**

```bash
.venv/bin/python scripts/migrate_stops_money_to_expenses.py --dry-run
```

Expected: payloads for each stop with amount > 0. Today's 坐火车(60 CNY) + 冰淇淋(25 CNY) + 任何 USD stops（前天的 QuickChek $6, Chick-fil-A $18 等）都在里面。

- [ ] **Step 5: Real run stops money migration**

```bash
.venv/bin/python scripts/migrate_stops_money_to_expenses.py
```

Expected: `done. created=N skipped=0`.

- [ ] **Step 6: Run verification tests**

```bash
.venv/bin/python -m pytest tests/test_expenses_migration.py -v
```

Expected: all tests pass. If any fails — STOP. Do not deploy Phase C.

---

## Phase C: Subtractive schema migrations (one deploy)

### Task 9: Migration 1779465627 — drop transactions

**Files:**
- Create: `pocketbase/pb_migrations/1779465627_drop_transactions.js`

- [ ] **Step 1: Write the migration**

Create `pocketbase/pb_migrations/1779465627_drop_transactions.js`:

```javascript
/// <reference path="../pb_data/types.d.ts" />
//
// Drop the legacy `transactions` collection. Safety check: every
// transaction row must already have a counterpart in expenses (by
// confirmation OR by date+description+amount). If anything looks
// unmigrated, throw and bail.
//
// Run scripts/migrate_transactions_to_expenses.py BEFORE deploying this.
//
// See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md.
//
migrate((app) => {
  let tx;
  try {
    tx = app.findCollectionByNameOrId("transactions");
  } catch (e) {
    return;
  }

  const txRows = app.findRecordsByFilter("transactions", "", "", 1000, 0);
  const unmigrated = [];
  for (const t of txRows) {
    const conf = (t.get("confirmation") || "").trim();
    let matches = [];
    if (conf) {
      matches = app.findRecordsByFilter(
        "expenses", `confirmation = "${conf}"`, "", 1, 0
      );
    } else {
      const date = (t.get("date") || "").substring(0, 10);
      const desc = (t.get("description") || "").replace(/"/g, '\\"');
      const amount = t.get("amount") || 0;
      matches = app.findRecordsByFilter(
        "expenses",
        `date >= "${date} 00:00:00" && date < "${date} 23:59:59" && description = "${desc}" && amount = ${amount}`,
        "", 1, 0
      );
    }
    if (matches.length === 0) {
      unmigrated.push(t.id);
    }
  }
  if (unmigrated.length > 0) {
    throw new Error(
      "Refusing to drop transactions: " + unmigrated.length +
      " row(s) not yet migrated to expenses. Run " +
      "scripts/migrate_transactions_to_expenses.py first. Sample ids: " +
      unmigrated.slice(0, 3).join(", ")
    );
  }

  app.delete(tx);
}, (app) => {
  const collection = new Collection({
    name: "transactions",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "description", type: "text", required: true, max: 500 },
      { name: "amount",      type: "number" },
      { name: "date",        type: "date" },
      { name: "type",        type: "select", maxSelect: 1, values: ["支出", "退款"] },
      { name: "category",    type: "select", maxSelect: 1,
        values: ["旅行", "订阅服务", "娱乐", "交通", "购物/日用", "餐饮"] },
      { name: "card",        type: "select", maxSelect: 1,
        values: ["Chase Sapphire Preferred (7675)"] },
      { name: "confirmation", type: "text" },
      { name: "source",      type: "select", maxSelect: 1, values: ["手动", "Gmail"] },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_tx_date     ON transactions (date)",
      "CREATE INDEX idx_tx_category ON transactions (category)",
      "CREATE UNIQUE INDEX idx_tx_confirmation ON transactions (confirmation) WHERE confirmation != ''",
    ],
  });
  app.save(collection);
});
```

- [ ] **Step 2: Commit**

```bash
git add pocketbase/pb_migrations/1779465627_drop_transactions.js
git commit -m "feat(pb): drop legacy transactions collection (gated)"
```

---

### Task 10: Migration 1779465628 — drop stops money fields

**Files:**
- Create: `pocketbase/pb_migrations/1779465628_drop_stops_money_fields.js`

- [ ] **Step 1: Write the migration**

Create `pocketbase/pb_migrations/1779465628_drop_stops_money_fields.js`:

```javascript
/// <reference path="../pb_data/types.d.ts" />
//
// Drop amount / currency / rate / amount_usd from stops. Safety check:
// every stop with amount > 0 must have ≥1 expense row pointing at it. If
// anything looks unmigrated, throw and bail.
//
// Run scripts/migrate_stops_money_to_expenses.py BEFORE deploying this.
//
// See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md.
//
migrate((app) => {
  const moneyStops = app.findRecordsByFilter("stops", "amount > 0", "", 10000, 0);
  const unmigrated = [];
  for (const s of moneyStops) {
    const matches = app.findRecordsByFilter("expenses", `stop = "${s.id}"`, "", 1, 0);
    if (matches.length === 0) {
      unmigrated.push(s.id);
    }
  }
  if (unmigrated.length > 0) {
    throw new Error(
      "Refusing to drop stops money fields: " + unmigrated.length +
      " stop(s) with amount > 0 have no linked expense. Run " +
      "scripts/migrate_stops_money_to_expenses.py first. Sample ids: " +
      unmigrated.slice(0, 3).join(", ")
    );
  }

  const c = app.findCollectionByNameOrId("stops");
  for (const name of ["amount", "currency", "rate", "amount_usd"]) {
    const f = c.fields.getByName(name);
    if (f) c.fields.removeById(f.id);
  }
  app.save(c);
}, (app) => {
  const c = app.findCollectionByNameOrId("stops");
  const specs = [
    { name: "amount",     type: "number" },
    { name: "currency",   type: "select", maxSelect: 1, values: ["JPY", "EUR", "USD", "CNY", "其他"] },
    { name: "rate",       type: "number" },
    { name: "amount_usd", type: "number" },
  ];
  for (const spec of specs) {
    if (!c.fields.getByName(spec.name)) {
      c.fields.add(new Field(spec));
    }
  }
  app.save(c);
});
```

- [ ] **Step 2: Commit**

```bash
git add pocketbase/pb_migrations/1779465628_drop_stops_money_fields.js
git commit -m "feat(pb): drop money fields from stops (gated)"
```

---

### Task 11: Deploy Phase C

- [ ] **Step 1: Fresh snapshot before deploy**

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -c "
from pathlib import Path
from notion_sync.pb_api import PBClient
from notion_sync.backup import backup_collections
path = backup_collections(PBClient(), Path('.bridge_data/backups'))
print('SNAPSHOT (pre-phase-C):', path)
"
```

- [ ] **Step 2: Deploy**

```powershell
deploy
```

PB restart applies migrations 1779465627 + 1779465628. Each safety check throws if data migration skipped — surfaces in journalctl.

- [ ] **Step 3: Verify**

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -c "
import requests
from notion_sync.pb_api import PBClient
pb = PBClient()
hdrs = {'Authorization': 'Bearer ' + pb.token}
r = requests.get(pb.base + '/api/collections/transactions', headers=hdrs)
print('transactions GET:', r.status_code, '(expect 404)')
r = requests.get(pb.base + '/api/collections/stops', headers=hdrs)
fnames = [f['name'] for f in r.json()['fields']]
for n in ['amount', 'currency', 'rate', 'amount_usd']:
    print(f'  stops.{n} present:', n in fnames, '(expect False)')
"
```

Expected:
- `transactions GET: 404`
- All 4 money fields → `present: False`

If anything still there — STOP, check journalctl.

---

## Phase D: Documentation + changelog

### Task 12: Update docs

**Files:**
- Modify: `docs/data-model.md`
- Modify: `CHANGELOG.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update docs/data-model.md**

Edit `docs/data-model.md`:

1. In §2 (PB side), after §2.7 add new §2.9 `expenses`:

```markdown
### 2.9 `expenses` (new in migration 1779465626)

Money child of stops/days/trips. Replaces the legacy `transactions`
collection (dropped in migration 1779465627).

\`\`\`
expenses {
  id, description (required, max 500),
  amount, currency (select 1) [USD, JPY, EUR, CNY, 其他],
  rate, amount_usd, date,
  type (select 1) [支出, 退款],
  expense_category (select 1) [旅行, 订阅服务, 娱乐, 交通, 购物/日用,
                               餐饮, 门票, 住宿, 代付, 其他],
  card (select 1) [Chase Sapphire Preferred (7675)],
  confirmation, source (select 1) [手动, Gmail, Agent],
  stop (relation→stops single, optional),
  day  (relation→days  single, optional),
  trip (relation→trips single, optional, denormalized = day.trip),
  notion_id, notion_last_edited, last_synced_at, created, updated
}

indexes:
  date, expense_category, stop, day, trip
  UNIQUE confirmation WHERE != ''
  UNIQUE notion_id    WHERE != ''
\`\`\`

**Conventions:**
- amount_usd auto-filled by writer (= amount for USD, = amount × rate otherwise)
- type=退款 stored with amount < 0 (so sum(amount_usd) is net spend)
- expense.trip = expense.day.trip when day has a trip (writer-side invariant)
- Relations are PB-only; Notion sync ignores them (per §8.1)
```

2. In §2.3 stops, delete the lines for `amount`, `currency`, `rate`, `amount_usd`. Add note: "**Removed in migration 1779465628** — all money fields moved to `expenses` (see §2.9)."

3. In §2.2 days, change `trip` line to: `trip                   relation→trips (single, OPTIONAL — relaxed in migration 1779465625)`

4. In §10 Quick reference: add row for expenses in the sync targets table (Notion DB id filled in PR2).

- [ ] **Step 2: Update CHANGELOG.md**

Add at top:

```markdown
## 2026-06-05 — Expenses redesign (PR1)

- New `expenses` PB collection: money child of stops/days/trips
- `transactions` collection dropped; 11 rows migrated
- `stops` money fields (amount/currency/rate/amount_usd) dropped; legacy rows migrated to expenses
- `days.trip` relaxed to optional (日常 day containers w/o trip)
- 代付 detection: description containing 代付 → expense_category=代付
- Auto day-backfill: legacy transactions get a day (created if needed)
- See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md and
  docs/superpowers/plans/2026-06-05-expenses-pr1-schema-migration.md
```

- [ ] **Step 3: Update CLAUDE.md**

Append to the "## Notion sync" section:

```markdown
PR4 shipped 2026-06-05: `transactions`→`expenses` redesign. expenses is
now a child of stops/days/trips; supports 日常 (no trip) and multi-expense
stops (e.g. 公园 = 门票 + 冰淇淋 + 水). See
**[docs/superpowers/specs/2026-06-05-expenses-redesign-design.md](docs/superpowers/specs/2026-06-05-expenses-redesign-design.md)**.
Sync wiring is in PR2 (separate plan).
```

- [ ] **Step 4: Commit**

```bash
git add docs/data-model.md CHANGELOG.md CLAUDE.md
git commit -m "docs: expenses redesign PR1 docs + changelog"
```

---

## Self-Review

Spec coverage check:

| Spec section | Plan coverage |
|---|---|
| Schema: expenses collection | Task 3 ✓ |
| Schema: stops bare | Task 10 ✓ |
| Schema: days.trip optional | Task 2 ✓ |
| Convention A: amount_usd auto-fill | PR2 (writer-side) ✓ |
| Convention B: refund negative | Task 5 (migration) + PR2 (writer-side) ✓ |
| Convention C: expense.trip = day.trip | Tasks 5/6 (migration) + PR2 (runtime invariant) ✓ |
| Migration a: transactions→expenses | Tasks 5 + 9 ✓ |
| Migration b: stops money→expenses | Tasks 6 + 10 ✓ |
| Migration c: drop stops money | Task 10 ✓ |
| Migration d: days.trip optional | Task 2 ✓ |
| 4 test stops note preserved | Task 7 ✓ |
| Backup snapshot before migration | Tasks 1 + 11.1 ✓ |
| Auto day-backfill by date | Task 5 ✓ |
| 代付 category | Tasks 3 + 5 + 7 ✓ |
| Sync wiring | DEFERRED to PR2 ✓ |
| Frontend/MCP/CHECKIN updates | DEFERRED to PR2 ✓ |

All PR1 requirements covered.
