# Timezone-Aware Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add IANA timezone fields across `locations / stops / days / expenses / foods` plus `due_at`+`due_tz` on `todos`, write a shared `resolve_tz()` helper, backfill existing rows, and update Notion sync to render datetimes with offsets — so future "明天3点提醒" feature anchors to the correct local time.

**Architecture:** Three layers. **(1) Schema** — one PB migration adds 7 new fields (TEXT max-64 for tz, datetime for `due_at`). **(2) Helper** — new `tz_resolver.py` exposes `resolve_tz(...)` fallback chain (location→GPS→day→phone) and `compute_due_at(local_date, local_time, tz)`; pure functions, fully unit-tested with `zoneinfo`. **(3) Sync** — `notion_sync/codec.py`'s `_pb_date_to_notion_start` accepts a `tz` hint; `transform.py` passes the row's tz column when present. Backfill is three idempotent scripts (one per layer of tables). Frontend reports `client_tz` per WS message; server stashes it on the agent prompt.

**Tech Stack:** PocketBase JS migrations, Python 3.11+ (`zoneinfo` stdlib + `timezonefinder` PyPI), pytest, FastAPI WebSocket, vanilla JS.

---

## File Map

| Path | Action | Responsibility |
|---|---|---|
| `requirements.txt` | Modify | Add `timezonefinder>=6.5` |
| `pocketbase/pb_migrations/1779465632_add_timezone_fields.js` | Create | All schema additions |
| `tz_resolver.py` | Create | `resolve_tz()` + `compute_due_at()` pure helpers |
| `tests/test_tz_resolver.py` | Create | Unit tests for above |
| `scripts/backfill_location_timezones.py` | Create | `locations.timezone` from lat/lng |
| `scripts/backfill_stop_timezones.py` | Create | `stops.timezone` + `days.timezone` |
| `scripts/backfill_child_timezones.py` | Create | `expenses.timezone` + `foods.timezone` from parent |
| `notion_sync/codec.py` (lines 19-44, 74-167) | Modify | `_pb_date_to_notion_start` accepts optional `tz` IANA → emit `+HH:MM` offset; plumb through `pb_field_to_notion_property` |
| `notion_sync/transform.py` | Modify | Look up tz column on the row and pass to codec for datetime fields |
| `tests/notion_sync/test_codec.py` | Modify | Add tests for offset rendering |
| `static/app.js` | Modify | Attach `client_tz` to every `user_message` payload |
| `server.py` (around line 2208 / `handle_ws_message` and line 356-372) | Modify | Read `client_tz`, stash on `state`, surface to agent via system prompt |
| `mcp_pb/SMARTNOTE_PROMPT.md` | Modify | Document tz writing rules for the agent |
| `docs/data-model.md` | Modify | Add tz columns + due_at/due_tz to each section, plus a new §X "Timezone resolution" subsection |
| `CLAUDE.md` | Modify | Brief pointer to the new tz design doc + behavioral note |
| `CHANGELOG.md` | Modify | One bullet per landed task |

---

## Task 1: Add `timezonefinder` dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Append `timezonefinder` to requirements**

Edit `requirements.txt`, add line at end (after `google-api-python-client>=2.130`):

```
# Timezone resolution from GPS coordinates (offline polygon lookup)
timezonefinder>=6.5
```

- [ ] **Step 2: Verify it installs locally**

```powershell
.\.venv\Scripts\pip install timezonefinder
.\.venv\Scripts\python -c "from timezonefinder import TimezoneFinder; print(TimezoneFinder().timezone_at(lng=139.6917, lat=35.6895))"
```

Expected: prints `Asia/Tokyo`.

- [ ] **Step 3: Commit**

```powershell
git add requirements.txt
git commit -m "deps: add timezonefinder for offline GPS to IANA lookup"
```

---

## Task 2: PB migration — add all tz fields

**Files:**
- Create: `pocketbase/pb_migrations/1779465632_add_timezone_fields.js`

- [ ] **Step 1: Write the migration**

Create the file with these exact contents (follows the same pattern as `1779465631_add_foods_relations_and_sync.js`):

```javascript
/// <reference path="../pb_data/types.d.ts" />
//
// Adds IANA timezone fields across the trip stack so cross-tz reminders
// and history can anchor to local time:
//
//   locations.timezone (text)        - computed from lat/lng at creation
//   stops.timezone     (text)        - denormalized from location or GPS
//   days.timezone      (text)        - inherited from day's first stop
//   expenses.timezone  (text)        - inherited from parent stop/day
//   foods.timezone     (text)        - inherited from parent stop/day
//   todos.due_at       (date)        - reminder trigger time (UTC)
//   todos.due_tz       (text)        - IANA name of user's intended tz
//
// See docs/superpowers/specs/2026-06-05-timezone-design.md.
//
migrate((app) => {
  const TXT_TABLES = ["locations", "stops", "days", "expenses", "foods"];
  for (const name of TXT_TABLES) {
    const c = app.findCollectionByNameOrId(name);
    if (c.fields.getByName("timezone")) continue;
    c.fields.add(new Field({
      name: "timezone",
      type: "text",
      max:  64,
    }));
    app.save(c);
  }

  const todos = app.findCollectionByNameOrId("todos");
  if (!todos.fields.getByName("due_at")) {
    todos.fields.add(new Field({
      name: "due_at",
      type: "date",
    }));
  }
  if (!todos.fields.getByName("due_tz")) {
    todos.fields.add(new Field({
      name: "due_tz",
      type: "text",
      max:  64,
    }));
  }
  const existing = todos.indexes || [];
  const idxDef = "CREATE INDEX idx_todos_due_at ON todos (due_at)";
  if (!existing.some((s) => s.includes("idx_todos_due_at"))) {
    todos.indexes = [...existing, idxDef];
  }
  app.save(todos);
}, (app) => {
  const TXT_TABLES = ["locations", "stops", "days", "expenses", "foods"];
  for (const name of TXT_TABLES) {
    const c = app.findCollectionByNameOrId(name);
    const f = c.fields.getByName("timezone");
    if (f) {
      c.fields.removeById(f.id);
      app.save(c);
    }
  }
  const todos = app.findCollectionByNameOrId("todos");
  for (const fname of ["due_at", "due_tz"]) {
    const f = todos.fields.getByName(fname);
    if (f) todos.fields.removeById(f.id);
  }
  todos.indexes = (todos.indexes || []).filter((s) => !s.includes("idx_todos_due_at"));
  app.save(todos);
});
```

- [ ] **Step 2: Apply locally (via deploy or manual copy) and verify schema**

After deploy (Task 12) applies the migration, verify via MCP `pb_get_collection` for each of 6 collections (locations, stops, days, expenses, foods, todos) — each should show the new field(s).

Expected: 5 collections show new `timezone` text field; `todos` shows `due_at` (date) + `due_tz` (text) + new index `idx_todos_due_at`.

- [ ] **Step 3: Commit**

```powershell
git add pocketbase/pb_migrations/1779465632_add_timezone_fields.js
git commit -m "feat(schema): add timezone fields to trip stack + due_at/due_tz to todos"
```

---

## Task 3: `tz_resolver` helper (TDD)

**Files:**
- Create: `tests/test_tz_resolver.py`
- Create: `tz_resolver.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tz_resolver.py`:

```python
"""Unit tests for tz_resolver - pure functions, no I/O."""
from datetime import date, time, datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tz_resolver import resolve_tz, compute_due_at, gps_to_tz


def test_resolve_uses_stop_tz_when_present():
    assert resolve_tz(stop={"timezone": "Asia/Tokyo"}) == "Asia/Tokyo"


def test_resolve_falls_back_to_gps_when_stop_has_no_tz():
    # Tokyo Station coords
    assert resolve_tz(stop={"timezone": ""}, lat=35.6812, lng=139.7671) == "Asia/Tokyo"


def test_resolve_falls_back_to_day_tz_when_no_stop_no_gps():
    assert resolve_tz(day={"timezone": "Europe/Paris"}) == "Europe/Paris"


def test_resolve_falls_back_to_phone_tz_last():
    assert resolve_tz(phone_tz="America/Los_Angeles") == "America/Los_Angeles"


def test_resolve_returns_none_when_nothing_known():
    assert resolve_tz() is None


def test_gps_to_tz_paris():
    assert gps_to_tz(lat=48.8566, lng=2.3522) == "Europe/Paris"


def test_gps_to_tz_handles_ocean_does_not_crash():
    # Middle of the Pacific. timezonefinder may return None or an Etc/* zone.
    # We accept any of those - the point is it doesn't crash.
    got = gps_to_tz(lat=0.0, lng=-160.0)
    assert got is None or got.startswith("Etc/") or got.startswith("Pacific/")


def test_compute_due_at_tokyo_3pm():
    # 2026-06-08 15:00 Tokyo = 2026-06-08 06:00 UTC
    got = compute_due_at(date(2026, 6, 8), time(15, 0), "Asia/Tokyo")
    assert got == datetime(2026, 6, 8, 6, 0, tzinfo=timezone.utc)


def test_compute_due_at_la_3pm_summer_dst():
    # 2026-06-08 15:00 LA (PDT, UTC-7) = 2026-06-08 22:00 UTC
    got = compute_due_at(date(2026, 6, 8), time(15, 0), "America/Los_Angeles")
    assert got == datetime(2026, 6, 8, 22, 0, tzinfo=timezone.utc)


def test_compute_due_at_la_3pm_winter_no_dst():
    # 2026-01-15 15:00 LA (PST, UTC-8) = 2026-01-15 23:00 UTC
    got = compute_due_at(date(2026, 1, 15), time(15, 0), "America/Los_Angeles")
    assert got == datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
.\.venv\Scripts\python -m pytest tests/test_tz_resolver.py -v
```

Expected: all tests FAIL with `ModuleNotFoundError: No module named 'tz_resolver'`.

- [ ] **Step 3: Implement `tz_resolver.py`**

Create `tz_resolver.py` at project root:

```python
"""Pure timezone-resolution helpers shared by writers, backfills, and the agent.

No I/O - all functions take primitives or PB-row dicts in and return values.
Caller is responsible for DB reads/writes.

Resolution order (see docs/superpowers/specs/2026-06-05-timezone-design.md §4):
    1. stop.timezone
    2. timezonefinder(lat, lng)
    3. day.timezone
    4. phone_tz
    5. None
"""
from __future__ import annotations

from datetime import date as _date, time as _time, datetime as _dt, timezone as _tz
from typing import Optional
from zoneinfo import ZoneInfo

try:
    from timezonefinder import TimezoneFinder
    _tf = TimezoneFinder()
except Exception:  # pragma: no cover - only hit if dep missing
    _tf = None


def gps_to_tz(*, lat: float, lng: float) -> Optional[str]:
    """Return IANA tz name for (lat, lng) or None if not resolvable."""
    if _tf is None:
        return None
    return _tf.timezone_at(lng=lng, lat=lat)


def resolve_tz(
    *,
    stop: Optional[dict] = None,
    day: Optional[dict] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    phone_tz: Optional[str] = None,
) -> Optional[str]:
    """Apply the fallback chain. Each input may be omitted; missing → skip."""
    if stop:
        s_tz = (stop.get("timezone") or "").strip()
        if s_tz:
            return s_tz
    if lat is not None and lng is not None:
        got = gps_to_tz(lat=lat, lng=lng)
        if got:
            return got
    if day:
        d_tz = (day.get("timezone") or "").strip()
        if d_tz:
            return d_tz
    if phone_tz:
        return phone_tz
    return None


def compute_due_at(local_date: _date, local_time: _time, tz_name: str) -> _dt:
    """Compose (date, time, IANA tz) into a UTC-aware datetime.

    DST handled by zoneinfo automatically. Raises ZoneInfoNotFoundError
    on bad tz_name - caller decides whether to fall back.
    """
    local_dt = _dt.combine(local_date, local_time).replace(tzinfo=ZoneInfo(tz_name))
    return local_dt.astimezone(_tz.utc)
```

- [ ] **Step 4: Run tests to verify they pass**

```powershell
.\.venv\Scripts\python -m pytest tests/test_tz_resolver.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add tz_resolver.py tests/test_tz_resolver.py
git commit -m "feat(tz): add resolve_tz/gps_to_tz/compute_due_at helpers + tests"
```

---

## Task 4: Backfill — `locations.timezone`

**Files:**
- Create: `scripts/backfill_location_timezones.py`
- Possibly modify: `notion_sync/pb_api.py` (add `update_record` if missing)

- [ ] **Step 1: Check `PBClient.update_record` exists**

Read `notion_sync/pb_api.py` and look for a method named `update_record`. If missing, append (place near other `_http` callers, match existing indentation):

```python
    def update_record(self, collection: str, rec_id: str, data: dict) -> dict:
        return self._http("PATCH", f"/api/collections/{collection}/records/{rec_id}", data)
```

- [ ] **Step 2: Write the backfill script**

Create `scripts/backfill_location_timezones.py`:

```python
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
```

- [ ] **Step 3: Commit**

```powershell
git add scripts/backfill_location_timezones.py notion_sync/pb_api.py
git commit -m "feat(backfill): fill locations.timezone from GPS (offline)"
```

(Actual run of the script is in Task 12.)

---

## Task 5: Backfill — `stops.timezone` and `days.timezone`

**Files:**
- Create: `scripts/backfill_stop_timezones.py`

- [ ] **Step 1: Write the script**

```python
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
```

- [ ] **Step 2: Commit**

```powershell
git add scripts/backfill_stop_timezones.py
git commit -m "feat(backfill): fill stops.timezone (location->GPS) and days.timezone (first stop)"
```

---

## Task 6: Backfill — `expenses.timezone` and `foods.timezone`

**Files:**
- Create: `scripts/backfill_child_timezones.py`

- [ ] **Step 1: Write the script**

```python
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
```

- [ ] **Step 2: Commit**

```powershell
git add scripts/backfill_child_timezones.py
git commit -m "feat(backfill): fill expenses/foods.timezone from parent stop/day"
```

---

## Task 7: Notion codec — datetime with offset (TDD)

**Files:**
- Modify: `tests/notion_sync/test_codec.py`
- Modify: `notion_sync/codec.py` (lines 19-44 and around line 74-167)

- [ ] **Step 1: Write the failing tests**

Append to `tests/notion_sync/test_codec.py`:

```python
def test_pb_date_to_notion_start_emits_offset_when_tz_given():
    from notion_sync.codec import _pb_date_to_notion_start
    # 2026-06-08 09:00 UTC in Asia/Tokyo (UTC+9) -> 18:00 local
    got = _pb_date_to_notion_start("2026-06-08 09:00:00.000Z", tz="Asia/Tokyo")
    assert got == "2026-06-08T18:00:00+09:00"


def test_pb_date_to_notion_start_no_tz_keeps_legacy_utc_z_format():
    from notion_sync.codec import _pb_date_to_notion_start
    got = _pb_date_to_notion_start("2026-06-08 09:00:00.000Z")
    assert got == "2026-06-08T09:00:00Z"


def test_pb_date_to_notion_start_date_only_passes_through():
    from notion_sync.codec import _pb_date_to_notion_start
    assert _pb_date_to_notion_start("2026-06-08") == "2026-06-08"
    assert _pb_date_to_notion_start("2026-06-08", tz="Asia/Tokyo") == "2026-06-08"


def test_pb_date_to_notion_start_dst_paris_winter():
    from notion_sync.codec import _pb_date_to_notion_start
    # 2026-01-15 12:00 UTC in Europe/Paris (CET, UTC+1) -> 13:00 local
    got = _pb_date_to_notion_start("2026-01-15 12:00:00.000Z", tz="Europe/Paris")
    assert got == "2026-01-15T13:00:00+01:00"


def test_pb_date_to_notion_start_dst_paris_summer():
    from notion_sync.codec import _pb_date_to_notion_start
    # 2026-06-15 12:00 UTC in Europe/Paris (CEST, UTC+2) -> 14:00 local
    got = _pb_date_to_notion_start("2026-06-15 12:00:00.000Z", tz="Europe/Paris")
    assert got == "2026-06-15T14:00:00+02:00"


def test_pb_date_to_notion_start_invalid_tz_falls_back_to_utc():
    from notion_sync.codec import _pb_date_to_notion_start
    got = _pb_date_to_notion_start("2026-06-08 09:00:00.000Z", tz="Not/AReal_Zone")
    assert got == "2026-06-08T09:00:00Z"


def test_pb_field_to_notion_property_date_with_tz_hint():
    from notion_sync.codec import pb_field_to_notion_property
    got = pb_field_to_notion_property("2026-06-08 09:00:00.000Z",
                                       pb_type="date", notion_type="date",
                                       tz="Asia/Tokyo")
    assert got == {"date": {"start": "2026-06-08T18:00:00+09:00"}}
```

- [ ] **Step 2: Run to verify they fail**

```powershell
.\.venv\Scripts\python -m pytest tests/notion_sync/test_codec.py -v -k "offset or dst or tz_hint or legacy_utc or date_only or invalid_tz"
```

Expected: TypeError (signature doesn't accept `tz`).

- [ ] **Step 3: Update `_pb_date_to_notion_start`**

Replace `notion_sync/codec.py` lines 19-44 with:

```python
def _pb_date_to_notion_start(value: Any, *, tz: str | None = None) -> str:
    """Convert a PB date/datetime string into the value for Notion's date.start.

    PB stores datetime in ``date`` fields as ``"YYYY-MM-DD HH:MM:SS.fffZ"``. When
    the HH:MM:SS portion is non-zero we emit a full ISO 8601 string so Notion
    treats the property as a datetime; when it's zero we emit ``YYYY-MM-DD`` so
    Notion keeps it as date-only. Returns "" for empty input.

    When ``tz`` is a valid IANA zone, the datetime is rendered with the matching
    ``+HH:MM`` offset so Notion shows it in that zone. Invalid or unknown ``tz``
    falls back to the legacy ``...Z`` (UTC) format.

    Edge case: a real event at exactly 00:00:00 UTC is indistinguishable from
    a date-only value and will be shown date-only in Notion. The full timestamp
    still round-trips through PB intact.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    s_norm = s.replace("T", " ")
    parts = s_norm.split(" ", 1)
    date_part = parts[0]
    if len(parts) < 2:
        return date_part
    time_part = parts[1].rstrip().rstrip("Z").rstrip()
    hms = time_part.split(".", 1)[0]
    if not hms or hms == "00:00:00":
        return date_part
    if tz:
        try:
            from datetime import datetime, timezone as _utc
            from zoneinfo import ZoneInfo
            utc_dt = datetime.strptime(f"{date_part} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_utc.utc)
            local_dt = utc_dt.astimezone(ZoneInfo(tz))
            return local_dt.isoformat(timespec="seconds")
        except Exception:
            pass
    return f"{date_part}T{hms}Z"
```

- [ ] **Step 4: Plumb `tz` through `pb_field_to_notion_property`**

Change the signature of `pb_field_to_notion_property` (around line 74) to accept an optional `tz` kwarg:

```python
def pb_field_to_notion_property(value: Any, *,
                                pb_type: str,
                                max_select: int = 1,
                                notion_type: str | None = None,
                                tz: str | None = None) -> dict:
```

Update the two `date` branches inside the function to pass `tz=tz`:

```python
        if notion_type == "date":
            if not s_value:
                return {"date": None}
            start = _pb_date_to_notion_start(s_value, tz=tz)
            return {"date": {"start": start}} if start else {"date": None}
```

```python
    if pb_type == "date":
        if not value:
            return {"date": None}
        start = _pb_date_to_notion_start(value, tz=tz)
        return {"date": {"start": start}} if start else {"date": None}
```

- [ ] **Step 5: Run tests to verify they pass**

```powershell
.\.venv\Scripts\python -m pytest tests/notion_sync/test_codec.py -v
```

Expected: all codec tests (old + new) PASS.

- [ ] **Step 6: Commit**

```powershell
git add notion_sync/codec.py tests/notion_sync/test_codec.py
git commit -m "feat(sync): codec emits Notion datetime with IANA offset when tz hint given"
```

---

## Task 8: Notion sync wiring — pass tz to codec from transform

**Files:**
- Modify: `notion_sync/transform.py`

- [ ] **Step 1: Locate the call site**

Open `notion_sync/transform.py` and find the function that loops PB fields and calls `pb_field_to_notion_property` (search for `pb_field_to_notion_property` in that file). Read 30 lines around it to identify the parameter name for the row dict and field-types map.

- [ ] **Step 2: Compute per-row tz hint, pass for date fields only**

At the top of the loop (before the per-field iteration), compute the row's tz **once**:

```python
    # Per-row tz hint for datetime fields. Priority: the row's own
    # `timezone` column (locations/stops/days/expenses/foods) OR `due_tz`
    # (todos). Empty disables the offset hint (legacy UTC).
    row_tz = (record.get("timezone") or record.get("due_tz") or "").strip() or None
```

(Replace `record` with the actual parameter name.)

In the field loop, where `pb_field_to_notion_property(...)` is called, pass `tz=row_tz` whenever the field is a date:

```python
        if field_type == "date":
            props[notion_name] = pb_field_to_notion_property(
                value, pb_type="date", notion_type=notion_type, tz=row_tz,
            )
        else:
            props[notion_name] = pb_field_to_notion_property(
                value, pb_type=field_type, max_select=max_sel, notion_type=notion_type,
            )
```

(Match the existing variable names — `field_type`, `notion_type`, `max_sel`, etc. — to whatever the file already uses.)

- [ ] **Step 3: Run existing tests**

```powershell
.\.venv\Scripts\python -m pytest tests/notion_sync/ -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```powershell
git add notion_sync/transform.py
git commit -m "feat(sync): transform pulls per-row timezone and passes to date codec"
```

---

## Task 9: Frontend `client_tz` reporting

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Find the `user_message` send site**

Search `static/app.js` for `'user_message'` or `"user_message"`. There's typically a single send call like `send({ type: 'user_message', text, ... })`.

- [ ] **Step 2: Compute `CLIENT_TZ` once at module scope**

Near the top of the IIFE/module (e.g. just after `// Claude Bridge web client`), add:

```javascript
  // Reported on every outgoing user_message so the agent can anchor
  // relative times ("明天 3 点") to the user's actual local timezone.
  const CLIENT_TZ = (() => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
    catch (_) { return ''; }
  })();
```

- [ ] **Step 3: Attach `client_tz` to the user_message payload**

At the send call:

```javascript
  send({ type: 'user_message', text, images, files, client_tz: CLIENT_TZ });
```

(Match the existing keys exactly — if `images`/`files` aren't in your current call, leave them out and just add `client_tz`.)

- [ ] **Step 4: Verify in browser devtools after deploy**

Open phone-bridge → DevTools → Network → WS frames → trigger a message → confirm outgoing frame contains `"client_tz": "<your tz>"`.

- [ ] **Step 5: Commit**

```powershell
git add static/app.js
git commit -m "feat(ui): report client_tz on every user_message frame"
```

---

## Task 10: Server-side passthrough + agent prompt

**Files:**
- Modify: `server.py` (around line 190 — state dataclass)
- Modify: `server.py` (around line 2208 — `handle_ws_message` `user_message` branch)
- Modify: `server.py` (around line 356-372 — system_prompt assembly)
- Modify: `mcp_pb/SMARTNOTE_PROMPT.md`

- [ ] **Step 1: Add `client_tz` to the state dataclass**

Find the dataclass field block (search for `websockets: set[WebSocket]`). Add adjacent:

```python
    client_tz: str = ""
```

- [ ] **Step 2: Stash `client_tz` from each user_message**

In `handle_ws_message`'s `user_message` branch (around line 2208-2217), add the parse + stash before the existing send:

```python
    if t == "user_message":
        text = (msg.get("text") or "").strip()
        images = msg.get("images") or []
        files = msg.get("files") or []
        client_tz = (msg.get("client_tz") or "").strip()
        if client_tz:
            state.client_tz = client_tz
        if not text and not images and not files:
            return
        await broadcast({
            "type": "user_echo", "text": text, "images": images, "files": files,
        })
        state.current_turn_task = asyncio.create_task(run_user_turn(text, images, files))
```

- [ ] **Step 3: Inject `client_tz` into the agent system prompt**

Find where `kwargs["system_prompt"]` is assembled (around line 356-372). At the end of that section, append:

```python
    if state.client_tz:
        tz_note = (
            f"\n\n[runtime] Current user timezone: {state.client_tz}. "
            f"When a user says relative times like '明天3点' or 'tomorrow 6pm', "
            f"resolve them per the rules in SMARTNOTE_PROMPT.md (Timezone section)."
        )
        sp = kwargs.get("system_prompt")
        if isinstance(sp, str):
            kwargs["system_prompt"] = sp + tz_note
        elif isinstance(sp, dict):
            kwargs["system_prompt"] = {
                **sp,
                "append": (sp.get("append", "") or "") + tz_note,
            }
```

(If the preset-append shape doesn't match the installed SDK version, fall back to prepending the tz note to the first user message in `run_user_turn`; the goal is that the agent sees `state.client_tz` each turn.)

- [ ] **Step 4: Document the writing rule for the agent**

Open `mcp_pb/SMARTNOTE_PROMPT.md` and append a new section:

```markdown
## Timezone

The runtime injects `Current user timezone: <IANA>` into your prompt every turn.
When writing rows that have a `timezone` column (locations, stops, days, expenses,
foods) or `todos.due_at`/`due_tz`, follow this rule:

1. **Resolve target date first.** "明天" = today's date in the runtime tz, +1.
2. **Pick the tz for the target date in this priority:**
   a. The stop(s) on that date (`stop.timezone`).
   b. The day on that date (`day.timezone`).
   c. A trip covering that date with stops → the latest stop on/before that date.
   d. The runtime `Current user timezone`.
3. **Compose `due_at` (UTC).** Local datetime in step-2 tz → convert to UTC via
   `zoneinfo`. Store as ISO `YYYY-MM-DDTHH:MM:SSZ`.
4. **Store `due_tz`.** Save the IANA name you used (e.g. `Asia/Tokyo`).
5. **For non-reminder rows** (creating a stop/expense/food), just set the row's
   `timezone` column following the same fallback. Don't write `due_at` on those.

Example: user is in Paris (runtime tz = `Europe/Paris`), says "after-tomorrow
6 PM dinner in Tokyo", and a Tokyo stop already exists for that date. Write
`todos.due_at = <UTC for 18:00 Asia/Tokyo on that date>`, `due_tz = "Asia/Tokyo"`.
```

- [ ] **Step 5: Commit**

```powershell
git add server.py mcp_pb/SMARTNOTE_PROMPT.md
git commit -m "feat(agent): plumb client_tz from WS to system prompt + document tz writing rules"
```

---

## Task 11: Doc updates

**Files:**
- Modify: `docs/data-model.md`
- Modify: `CLAUDE.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update `data-model.md` per-table schema blocks**

For each of `locations`, `stops`, `days`, `expenses`, `foods`, find the fenced schema block and add inside it (matching existing column alignment):

```
  timezone               text, max 64   // IANA name; see Timezone section
```

For `todos`, add:

```
  due_at                 date           // reminder trigger (UTC)
  due_tz                 text, max 64   // IANA tz user expressed time in
```

- [ ] **Step 2: Add a new "Timezone resolution" section at end of data-model.md**

Append:

```markdown
## §X Timezone resolution

All trip-stack collections carry an optional `timezone` column (IANA name).
Writer-side fallback chain (see
`docs/superpowers/specs/2026-06-05-timezone-design.md`):

1. `stop.timezone` — explicit on the stop (denormalized from location at write time)
2. `gps_to_tz(stop.actual_lat, stop.actual_lng)` — when GPS present
3. `day.timezone` — inherited from day
4. runtime client tz reported by phone-bridge
5. empty (leave for later patching)

Reminders (`todos.due_at` UTC + `todos.due_tz` IANA) anchor to the resolved tz
at write time; subsequent edits to the trip's tz do **not** retroactively
shift existing `due_at` values (the original intent is preserved by `due_tz`).

Notion-side: datetime columns are rendered with the row's `timezone` (or
`due_tz` for todos) as a `+HH:MM` offset so users see local time in Notion
directly. The IANA name itself is also synced as a plain text column.
```

(Number §X to fit the existing section numbering in the file.)

- [ ] **Step 3: Update `CLAUDE.md`**

Add this subsection right after the existing stops-redesign block:

```markdown
### Timezone-aware data (shipped 2026-06-05)

All trip-stack collections now carry `timezone` (IANA name) — `locations`,
`stops`, `days`, `expenses`, `foods`. `todos` carries `due_at` (UTC) +
`due_tz` for cross-tz reminders. See
**[docs/superpowers/specs/2026-06-05-timezone-design.md](docs/superpowers/specs/2026-06-05-timezone-design.md)**
for the design and `tz_resolver.py` for the shared helpers.

Frontend reports `client_tz` per WS message; server stashes on
`state.client_tz` and injects it into the agent system prompt. Agents follow
the fallback chain documented in `mcp_pb/SMARTNOTE_PROMPT.md` (Timezone
section).
```

- [ ] **Step 4: Update `CHANGELOG.md`**

Add at top under today's date:

```markdown
## 2026-06-05

- feat(schema): add timezone fields to locations/stops/days/expenses/foods
  and due_at/due_tz to todos for cross-tz reminders
- feat(tz): tz_resolver helper + offline GPS→IANA via timezonefinder
- feat(backfill): three idempotent scripts populate tz on existing rows
- feat(sync): Notion datetime columns rendered with row's tz as +HH:MM offset
- feat(agent): client_tz piped from WS into system prompt
```

- [ ] **Step 5: Commit**

```powershell
git add docs/data-model.md CLAUDE.md CHANGELOG.md
git commit -m "docs: timezone-aware data model + agent writing rules"
```

---

## Task 12: Deploy + smoke verify

**Files:** (none — deploy + observe + run backfills)

- [ ] **Step 1: Deploy**

```powershell
deploy
```

Wait for `phone-bridge` health to report green.

- [ ] **Step 2: Verify migration applied on the VM**

```bash
ssh dashboard-server "ls /opt/pocketbase/pb_migrations/ | grep 1779465632"
ssh dashboard-server "sudo journalctl -u pocketbase -n 50 --no-pager | tail -30"
```

Expected: migration file present; PB logs show migration apply success.

Then verify via MCP `pb_get_collection` on each of locations / stops / days / expenses / foods / todos → new fields present.

- [ ] **Step 3: Dry-run all three backfills, then real run**

```bash
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python scripts/backfill_location_timezones.py --dry-run"
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python scripts/backfill_location_timezones.py"

ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python scripts/backfill_stop_timezones.py --dry-run"
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python scripts/backfill_stop_timezones.py"

ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python scripts/backfill_child_timezones.py --dry-run"
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python scripts/backfill_child_timezones.py"
```

Expected: each prints `patched=N skipped=M` with non-zero `patched` for locations and (likely) stops.

- [ ] **Step 4: Spot-check via MCP**

`mcp__pb__pb_search` collection=`locations`, filter=`timezone!=''`, limit=5 — should return 5 rows.
`mcp__pb__pb_search` collection=`days`, filter=`timezone!=''`, limit=5 — should return rows that had stops.

- [ ] **Step 5: Agent end-to-end smoke**

In phone-bridge browser chat say:

> 创建一个 todo：明天下午3点提醒我喝水

Then `mcp__pb__pb_search` collection=`todos`, sort=`-created`, limit=1:
- `due_at` should be **tomorrow 15:00 in your phone tz**, expressed as UTC.
- `due_tz` should equal your phone IANA name.

If the agent didn't write `due_at`/`due_tz`, re-read `mcp_pb/SMARTNOTE_PROMPT.md` and confirm Task 10 step 3 properly inserted `state.client_tz` into the prompt.

- [ ] **Step 6: Notion sync check**

```bash
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python -m notion_sync.runner --force-now --only todos"
```

Open the synced todo in Notion — `Due At` column should show the local time (e.g. "Jun 6, 2026 3:00 PM"), not UTC.

- [ ] **Step 7: Final status note**

If smoke-test surfaces edge cases that need follow-up, append `## Open Items` notes to the spec — do not fix in-place. Otherwise no commit needed for this task.

---

## Self-Review Notes

- Each new file path verified against actual repo layout via Glob.
- `_pb_date_to_notion_start` signature change is backward-compatible (`tz` is kwarg-only with default `None`).
- Tests use `zoneinfo` directly so they pass on Windows (Py 3.11+ ships system zoneinfo; if a target machine lacks tzdata, add `tzdata` to requirements).
- Task 10's preset-system-prompt branch is best-effort — the SDK API may differ; if so, fall back to prepending the tz note into the first user message in `run_user_turn`.
- No task touches `transactions` (already dropped in `1779465627`).
- Frontend Task 9 computes `CLIENT_TZ` once at module scope; if the user changes their device tz mid-session the value won't refresh until page reload — acceptable for v1.
- The fallback chain in `tz_resolver.resolve_tz` matches the spec §4 order exactly: stop → GPS → day → phone. Stops without GPS but with a `location` already covered (caller passes `stop_proxy` with location's tz).
- All backfills are idempotent — safe to rerun. Skipped rows are explicitly counted.

---

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-06-05-timezone-impl.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — I run tasks in this session using executing-plans, batch with checkpoints.

Which approach?
