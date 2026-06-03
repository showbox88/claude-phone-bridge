# Notion ↔ PB Sync — PR2: Daily Cron Runner

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire up the daily background sync. Each hour systemd fires `notion_sync.runner`; the runner checks whether the local hour in the configured timezone matches `sync_global.sync_hour_local`; if yes it walks every enabled `sync_config` row and performs sync. Single-side changes / new rows are pushed and logged to Sync Activity as `Auto-applied`. Conflicts (both sides changed) and deletions (one side disappeared) are **detected and enqueued to Sync Activity with `decision=Pending`** so the user can review snapshots and pick a winner. PR2 does **not** apply user-set decisions yet (that's PR3's "decision applier"), but the queue is populated and visible. Operational events (run start/end, errors) go to `.bridge_data/sync.log`.

**Architecture:** A new `notion_sync/runner.py` is the entry point. It pulls "changes since last_synced_at" from both sides per collection, joins on `pb_id` / `notion_id`, and routes each row into one of: skip (no change), auto-apply PB→Notion, auto-apply Notion→PB, conflict-enqueue, delete-enqueue. A new `notion_sync/changeset.py` holds the pure categorizer logic (heavily tested). `notion_sync/activity.py` gets a small `pending_action_exists()` helper so re-detected conflicts/deletes don't duplicate-write to Sync Activity. Two systemd unit files (`notion-sync.service` + `notion-sync.timer`) install via a deploy hook. Operational logs (run boundaries, apply errors) go to `.bridge_data/sync.log`.

**Tech Stack:** Same as PR1 — Python stdlib + the existing `notion_sync` modules. `zoneinfo` (stdlib, Python 3.9+) for timezone handling. systemd `oneshot` service + hourly timer.

**Spec reference:** `docs/superpowers/specs/2026-06-02-notion-pb-sync-design.md` — see "同步流程" and "渐进上线 PR2 row".

---

## File Structure

**Created:**
- `notion_sync/changeset.py` — pure logic: given pb_rows + notion_rows + last_synced_at, categorize each row into `NoChange` / `PbOnlyChange` / `NotionOnlyChange` / `BothChanged` / `PbNew` / `NotionNew` / `NotionVanished` / `PbVanished`.
- `notion_sync/transform.py` — shared row transforms moved out of `reconcile_initial.py` so `runner.py` can reuse them.
- `notion_sync/runner.py` — the orchestrator. Reads sync_global, time-guards, walks sync_config rows, dispatches per-categorization, writes Auto-applied to Sync Activity, enqueues conflicts/deletes to Sync Activity with `decision=Pending`.
- `notion_sync/logger.py` — small wrapper that writes structured JSON lines to `.bridge_data/sync.log` for operational events only (run boundaries, per-action errors). Conflicts/deletes do NOT go here — they go to Sync Activity.
- `tests/notion_sync/test_changeset.py` — table-driven tests covering every categorization branch.
- `tests/notion_sync/test_runner_guard.py` — tests for the timezone-guard logic only (pure function; doesn't hit PB/Notion).
- `deploy/notion-sync.service` — systemd service unit (template; deployed to `/etc/systemd/system/`).
- `deploy/notion-sync.timer` — systemd timer unit.
- `deploy/install_systemd.sh` — one-shot installer (idempotent: copies units, `systemctl daemon-reload`, enables + starts timer).

**Modified:**
- `notion_sync/__init__.py` — bump the docstring to reflect PR2 contents.
- `notion_sync/activity.py` — add one small helper `pending_action_exists()` for idempotent enqueue (checks if a Pending row already exists for a given pb_id/notion_id/op).
- `scripts/reconcile_initial.py` — replace the three transform functions with `from notion_sync.transform import ...` (refactor only — same behavior).
- `CLAUDE.md` — replace the "PR1 baseline" note with a fuller "Notion sync" section covering daily operation, where conflicts appear, how to pause, how to force a manual run.

**No changes:** `notion_sync/codec.py`, `matching.py`, `backup.py`, `pb_api.py`, `notion_api.py`, `scripts/setup_notion_sync_db.py`, `server.py`, `pb_tools.py`.

---

## Pre-Task Setup

- [ ] **Step 0: Sanity-check PR1 state on the VM**

Run:
```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python -c 'from notion_sync.pb_api import PBClient; pb = PBClient(); cs = pb.list_records(\"sync_config\", filter=\"enabled=true\", sort=\"\"); print(len(cs), \"enabled sync_config rows\")'"
```

Expected: `6 enabled sync_config rows`. If not, PR1 isn't fully in place.

Also confirm `NOTION_SYNC_ACTIVITY_DB_ID` is set in VM `.env`:
```powershell
ssh dashboard-server "grep -c '^NOTION_SYNC_ACTIVITY_DB_ID=' /home/dev/phone-bridge/.env"
```
Expected: `1`.

---

## Task 1: `notion_sync/changeset.py` — pure categorization logic (TDD)

**Files:**
- Create: `notion_sync/changeset.py`
- Create: `tests/notion_sync/test_changeset.py`

The whole point of a separate module: this can be exhaustively unit-tested without any I/O. The runner becomes a thin shell that calls this and dispatches.

### Categories (returned as `Action` dataclass instances)

| Action | When |
|---|---|
| `NoChange(pb_id, notion_id)` | Linked pair; both timestamps ≤ last_synced_at |
| `PbOnlyChange(pb_row, notion_id)` | Linked; PB.updated > last_synced_at; notion.last_edited_time ≤ pb_row.notion_last_edited |
| `NotionOnlyChange(notion_page, pb_id)` | Linked; notion.last_edited_time > pb_row.notion_last_edited; PB.updated ≤ last_synced_at |
| `BothChanged(pb_row, notion_page)` | Linked; both changed (conflict — log only in PR2) |
| `PbNew(pb_row)` | PB row with empty notion_id |
| `NotionNew(notion_page)` | Notion page with empty pb_id |
| `NotionVanished(pb_row)` | PB row has notion_id but that page isn't in Notion's current fetch (delete-log only in PR2) |
| `PbVanished(notion_page)` | Notion page has pb_id but that PB id isn't in PB's current fetch |

### Step 1: Write failing tests

`tests/notion_sync/test_changeset.py`:
```python
"""Tests for changeset categorization. Every branch covered."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.changeset import (
    NoChange,
    PbOnlyChange,
    NotionOnlyChange,
    BothChanged,
    PbNew,
    NotionNew,
    NotionVanished,
    PbVanished,
    categorize,
)


def _pb(id_, *, notion_id="", updated="2026-06-01 00:00:00.000Z",
        notion_last_edited=""):
    return {"id": id_, "notion_id": notion_id, "updated": updated,
            "notion_last_edited": notion_last_edited}


def _notion(id_, *, pb_id="", last_edited_time="2026-06-01T00:00:00.000Z"):
    rt = [{"plain_text": pb_id}] if pb_id else []
    return {"id": id_, "last_edited_time": last_edited_time,
            "properties": {"pb_id": {"type": "rich_text", "rich_text": rt}}}


def test_no_change_when_neither_side_moved():
    last = "2026-06-02 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-01 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-01T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert len(actions) == 1
    assert isinstance(actions[0], NoChange)


def test_pb_only_change():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-02 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-01T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert isinstance(actions[0], PbOnlyChange)
    assert actions[0].pb_row["id"] == "p1"
    assert actions[0].notion_id == "n1"


def test_notion_only_change():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-01 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-02T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert isinstance(actions[0], NotionOnlyChange)


def test_both_changed():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-02 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-02T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert isinstance(actions[0], BothChanged)


def test_pb_new_unlinked():
    pb_rows = [_pb("p2")]
    notion_rows = []
    actions = categorize(pb_rows, notion_rows, last_synced_at="2026-06-01 00:00:00.000Z")
    assert isinstance(actions[0], PbNew)


def test_notion_new_unlinked():
    pb_rows = []
    notion_rows = [_notion("n2")]
    actions = categorize(pb_rows, notion_rows, last_synced_at="2026-06-01 00:00:00.000Z")
    assert isinstance(actions[0], NotionNew)


def test_notion_vanished_pb_thinks_linked():
    pb_rows = [_pb("p1", notion_id="n_gone")]
    notion_rows = []
    actions = categorize(pb_rows, notion_rows, last_synced_at="2026-06-01 00:00:00.000Z")
    assert isinstance(actions[0], NotionVanished)


def test_pb_vanished_notion_thinks_linked():
    pb_rows = []
    notion_rows = [_notion("n1", pb_id="p_gone")]
    actions = categorize(pb_rows, notion_rows, last_synced_at="2026-06-01 00:00:00.000Z")
    assert isinstance(actions[0], PbVanished)


def test_mixed_set():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [
        _pb("p1", notion_id="n1",
            updated="2026-06-02 00:00:00.000Z",
            notion_last_edited="2026-06-01T00:00:00.000Z"),
        _pb("p2"),
        _pb("p3", notion_id="n_gone"),
    ]
    notion_rows = [
        _notion("n1", pb_id="p1",
                last_edited_time="2026-06-01T00:00:00.000Z"),
        _notion("n2"),
        _notion("n3", pb_id="p_gone"),
    ]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    kinds = sorted(type(a).__name__ for a in actions)
    assert kinds == sorted([
        "PbOnlyChange", "PbNew", "NotionVanished",
        "NotionNew", "PbVanished",
    ])


def test_iso_t_separator_normalized():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-02 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-01T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert isinstance(actions[0], PbOnlyChange)
```

### Step 2: Verify they fail

```powershell
python -m pytest tests/notion_sync/test_changeset.py -v
```
Expected: `ModuleNotFoundError: No module named 'notion_sync.changeset'`.

### Step 3: Write the implementation

`notion_sync/changeset.py`:
```python
"""Categorize PB and Notion rows into sync actions.

Pure function — no I/O, no globals. Given the rows on both sides and the
last_synced_at timestamp for the collection, returns a list of Action
dataclass instances. The runner does the I/O dispatch.

Timestamp comparison strategy: normalize the T separator and timezone
suffix to a uniform 'YYYY-MM-DD HH:MM:SS.SSSZ' form so lexicographic
order matches chronological order.
"""
from __future__ import annotations

from dataclasses import dataclass


def _norm_ts(s) -> str:
    if not s:
        return ""
    s = str(s).replace("T", " ")
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    return s


def _pb_id_from_notion(page: dict) -> str:
    prop = page.get("properties", {}).get("pb_id", {})
    return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))


@dataclass
class Action:
    pass


@dataclass
class NoChange(Action):
    pb_id: str
    notion_id: str


@dataclass
class PbOnlyChange(Action):
    pb_row: dict
    notion_id: str


@dataclass
class NotionOnlyChange(Action):
    notion_page: dict
    pb_id: str


@dataclass
class BothChanged(Action):
    pb_row: dict
    notion_page: dict


@dataclass
class PbNew(Action):
    pb_row: dict


@dataclass
class NotionNew(Action):
    notion_page: dict


@dataclass
class NotionVanished(Action):
    pb_row: dict


@dataclass
class PbVanished(Action):
    notion_page: dict


def categorize(pb_rows: list[dict],
               notion_rows: list[dict],
               *,
               last_synced_at: str) -> list[Action]:
    last = _norm_ts(last_synced_at)
    notion_by_id = {p["id"]: p for p in notion_rows}
    pb_by_id = {r["id"]: r for r in pb_rows}

    actions: list[Action] = []
    handled_notion_ids: set[str] = set()

    for pb_row in pb_rows:
        notion_id = pb_row.get("notion_id") or ""
        if not notion_id:
            actions.append(PbNew(pb_row=pb_row))
            continue

        notion_page = notion_by_id.get(notion_id)
        if notion_page is None:
            actions.append(NotionVanished(pb_row=pb_row))
            continue

        handled_notion_ids.add(notion_id)

        pb_updated = _norm_ts(pb_row.get("updated"))
        seen_notion_edit = _norm_ts(pb_row.get("notion_last_edited"))
        notion_edited = _norm_ts(notion_page.get("last_edited_time"))

        pb_changed = pb_updated > last
        notion_changed = (notion_edited > seen_notion_edit
                          if seen_notion_edit else notion_edited > last)

        if pb_changed and notion_changed:
            actions.append(BothChanged(pb_row=pb_row, notion_page=notion_page))
        elif pb_changed:
            actions.append(PbOnlyChange(pb_row=pb_row, notion_id=notion_id))
        elif notion_changed:
            actions.append(NotionOnlyChange(notion_page=notion_page, pb_id=pb_row["id"]))
        else:
            actions.append(NoChange(pb_id=pb_row["id"], notion_id=notion_id))

    for notion_page in notion_rows:
        if notion_page["id"] in handled_notion_ids:
            continue
        pb_id = _pb_id_from_notion(notion_page)
        if not pb_id:
            actions.append(NotionNew(notion_page=notion_page))
        elif pb_id not in pb_by_id:
            actions.append(PbVanished(notion_page=notion_page))
        else:
            pb_row = pb_by_id[pb_id]
            notion_edited = _norm_ts(notion_page.get("last_edited_time"))
            seen_notion_edit = _norm_ts(pb_row.get("notion_last_edited"))
            notion_changed = (notion_edited > seen_notion_edit
                              if seen_notion_edit else notion_edited > last)
            if notion_changed:
                actions.append(NotionOnlyChange(notion_page=notion_page, pb_id=pb_id))
            else:
                actions.append(NoChange(pb_id=pb_id, notion_id=notion_page["id"]))

    return actions
```

### Step 4: Verify tests pass

```powershell
python -m pytest tests/notion_sync/test_changeset.py -v
```
Expected: all 10 tests pass.

### Step 5: Commit

```powershell
git add notion_sync/changeset.py tests/notion_sync/test_changeset.py
git commit -m "$(cat <<'EOF'
PR2: notion_sync.changeset — pure row categorizer + tests

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `notion_sync/logger.py` — operational event log

**Files:**
- Create: `notion_sync/logger.py`

Only for *operational* events: run boundaries, apply errors, paused-skip, bad-timezone. Conflicts and deletes go to Sync Activity (next task), not here.

### Step 1: Write the implementation

```python
"""Structured operational event log for the sync runner.

One JSON line per significant operational event (run_start, run_end,
apply_error, skipped_paused, bad_timezone). Tail-able.

Conflicts and deletions are NOT written here — they go to the Sync
Activity Notion DB via notion_sync.activity helpers so the user can
review snapshots and pick a winner.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _log_path() -> Path:
    root = Path(os.environ.get("BRIDGE_DATA_DIR", ".bridge_data"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "sync.log"


def log_event(event: str, **fields) -> None:
    """Append a JSON line. `event` is the discriminator
    (e.g. 'run_start', 'run_end', 'apply_error', 'skipped_paused')."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "event": event, **fields}
    line = json.dumps(rec, ensure_ascii=False)
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(line + "\n")
```

### Step 2: Commit

```powershell
git add notion_sync/logger.py
git commit -m "$(cat <<'EOF'
PR2: notion_sync.logger — operational event log writer

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2b: Add `pending_action_exists()` to `notion_sync/activity.py`

**Files:**
- Modify: `notion_sync/activity.py`

The same conflict will be re-detected on every cron tick until the user resolves it. To avoid spamming Sync Activity with N copies of the same conflict, the runner queries first and skips if a Pending row already exists for that pb_id/notion_id/op.

### Step 1: Append this function to `notion_sync/activity.py`

Add after the existing `write_delete_question` function:

```python
def pending_action_exists(client, *, op: str, pb_id: str = "",
                          notion_id: str = "") -> bool:
    """True iff Sync Activity already has a Pending row for this
    pb_id/notion_id/op combination. Used to make enqueue idempotent.

    At least one of pb_id / notion_id must be non-empty.
    """
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    clauses = [
        {"property": "op",       "select":    {"equals": op}},
        {"property": "decision", "select":    {"equals": "Pending"}},
    ]
    if pb_id:
        clauses.append({"property": "pb_id",
                        "rich_text": {"equals": pb_id}})
    if notion_id:
        clauses.append({"property": "notion_id",
                        "rich_text": {"equals": notion_id}})
    body = {"filter": {"and": clauses}, "page_size": 1}
    rows = client.query_database(db_id, filter_=body["filter"], page_size=1)
    return len(rows) > 0
```

### Step 2: Smoke-verify it imports

```powershell
python -c "from notion_sync.activity import pending_action_exists; print('ok')"
```

### Step 3: Commit

```powershell
git add notion_sync/activity.py
git commit -m "$(cat <<'EOF'
PR2: activity.pending_action_exists for idempotent conflict/delete enqueue

Re-detected conflicts must not duplicate-write to Sync Activity. The
runner queries this before each write_conflict / write_delete_question.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Extract shared transforms into `notion_sync/transform.py` (refactor)

**Files:**
- Create: `notion_sync/transform.py`
- Modify: `scripts/reconcile_initial.py`

### Step 1: Write `notion_sync/transform.py`

```python
"""PB ↔ Notion row-level transforms — shared by reconcile_initial and runner."""
from __future__ import annotations

from notion_sync.codec import (
    notion_property_to_pb_field,
    pb_field_to_notion_property,
    snake_to_title,
    title_to_snake,
)
from notion_sync.pb_api import PBClient


def collection_field_types(pb: PBClient, name: str) -> dict[str, dict]:
    for c in pb.list_collections():
        if c["name"] == name:
            return {
                f["name"]: {"type": f["type"], "maxSelect": f.get("maxSelect", 1)}
                for f in c.get("fields", [])
            }
    raise RuntimeError(f"collection not found: {name}")


def notion_page_to_pb_dict(page: dict, field_types: dict[str, dict],
                           overrides: dict[str, str]) -> dict:
    out: dict = {}
    for prop_name, prop_val in page.get("properties", {}).items():
        pb_name = overrides.get(prop_name, title_to_snake(prop_name))
        if pb_name not in field_types:
            continue
        spec = field_types[pb_name]
        out[pb_name] = notion_property_to_pb_field(
            prop_val, pb_type=spec["type"], max_select=spec.get("maxSelect", 1)
        )
    return out


def pb_record_to_notion_props(record: dict, field_types: dict[str, dict],
                              overrides_inv: dict[str, str],
                              title_field: str,
                              notion_schema: dict[str, dict]) -> dict:
    SKIP = {"id", "created", "updated", "collectionId", "collectionName",
            "expand", "notion_id", "notion_last_edited", "last_synced_at"}
    notion_by_snake = {title_to_snake(name): name for name in notion_schema}
    title_prop_name = next(
        (n for n, s in notion_schema.items() if s.get("type") == "title"),
        None,
    )

    props: dict = {}
    for pb_name, value in record.items():
        if pb_name in SKIP:
            continue
        if pb_name not in field_types:
            continue
        if pb_name == title_field:
            continue
        spec = field_types[pb_name]
        notion_name = overrides_inv.get(pb_name) or notion_by_snake.get(pb_name)
        if not notion_name or notion_name not in notion_schema:
            continue
        notion_type = notion_schema[notion_name].get("type")
        props[notion_name] = pb_field_to_notion_property(
            value,
            pb_type=spec["type"],
            max_select=spec.get("maxSelect", 1),
            notion_type=notion_type,
        )

    if title_prop_name is not None:
        title_val = record.get(title_field, "") or ""
        props[title_prop_name] = {"title": [{"type": "text",
                                              "text": {"content": str(title_val)[:200]}}]}

    return props
```

### Step 2: Update `scripts/reconcile_initial.py`

Find the three function definitions (`collection_field_types`, `notion_page_to_pb_dict`, `pb_record_to_notion_props`) and delete them. Add an import near the top (with the other `from notion_sync...` imports):

```python
from notion_sync.transform import (
    collection_field_types,
    notion_page_to_pb_dict,
    pb_record_to_notion_props,
)
```

### Step 3: Verify reconcile still works (syntax + unit tests)

```powershell
python -c "import ast; ast.parse(open('scripts/reconcile_initial.py', encoding='utf-8').read()); print('syntax ok')"
python -m pytest tests/notion_sync/ -q
```
Expected: syntax ok, 41 pre-existing tests still pass.

### Step 4: Commit

```powershell
git add notion_sync/transform.py scripts/reconcile_initial.py
git commit -m "$(cat <<'EOF'
PR2: extract row transforms into notion_sync.transform for runner reuse

Refactor only — reconcile_initial.py imports the same three functions
from the package instead of defining them locally.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `notion_sync/runner.py` — the orchestrator

**Files:**
- Create: `notion_sync/runner.py`
- Modify: `notion_sync/__init__.py` (docstring only)

### Step 1: Write the runner

`notion_sync/runner.py`:
```python
#!/usr/bin/env python3
"""Daily sync runner.

systemd fires this every hour. The runner checks whether the local time
in the configured timezone matches sync_global.sync_hour_local; if yes it
performs one sync pass. Otherwise it exits silently.

Single-side changes auto-sync and are logged to Sync Activity as
'Auto-applied'. Conflicts (both sides changed) and deletions (one side's
ID disappeared) are detected and JSON-logged but NOT enqueued — that
behavior lands in PR3.

Run manually for testing:
    python -m notion_sync.runner --force-now
    python -m notion_sync.runner --force-now --only trips
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from notion_sync.activity import (
    pending_action_exists,
    write_auto_applied,
    write_conflict,
    write_delete_question,
)
from notion_sync.changeset import (
    BothChanged,
    NoChange,
    NotionNew,
    NotionOnlyChange,
    NotionVanished,
    PbNew,
    PbOnlyChange,
    PbVanished,
    categorize,
)
from notion_sync.logger import log_event
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.transform import (
    collection_field_types,
    notion_page_to_pb_dict,
    pb_record_to_notion_props,
)


TITLE_FIELD_BY_COLLECTION = {
    "trips": "title", "plans": "title", "todos": "title",
    "days":  "name",  "contacts": "name", "locations": "name",
}


def now_iso_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_iso_datetime() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def should_run_now(sync_global: dict, *, now_utc: datetime | None = None) -> bool:
    """True iff the local hour in sync_global.timezone == sync_hour_local
    AND sync_global.paused is False. Returns False if paused or off-hour.
    Tolerant of missing config (defaults UTC, hour=3).
    """
    if sync_global.get("paused"):
        return False
    tz_name = sync_global.get("timezone") or "UTC"
    target_hour = int(sync_global.get("sync_hour_local") or 3)
    now = now_utc or datetime.now(timezone.utc)
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:
        log_event("bad_timezone", configured=tz_name)
        return False
    return local.hour == target_hour


def _pb_id_from_notion(page: dict) -> str:
    prop = page.get("properties", {}).get("pb_id", {})
    return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))


def _apply_pb_to_notion(action: PbOnlyChange, *,
                        collection: str,
                        field_types: dict,
                        overrides_inv: dict,
                        title_field: str,
                        notion_schema: dict,
                        pb: PBClient, nc: NotionClient) -> None:
    r = action.pb_row
    props = pb_record_to_notion_props(r, field_types, overrides_inv,
                                       title_field, notion_schema)
    props["last_synced_at"] = {"date": {"start": now_iso_date()}}
    page = nc.update_page(action.notion_id, properties=props)
    pb.update_record(collection, r["id"], {
        "notion_last_edited": page.get("last_edited_time"),
        "last_synced_at": now_iso_datetime(),
    })
    write_auto_applied(nc, collection=collection,
                       direction="PB→Notion",
                       summary=str(r.get(title_field, ""))[:80],
                       pb_id=r["id"], notion_id=action.notion_id,
                       record_link=page.get("url"))


def _apply_notion_to_pb(action: NotionOnlyChange, *,
                        collection: str,
                        field_types: dict,
                        overrides: dict,
                        title_field: str,
                        pb: PBClient, nc: NotionClient) -> None:
    npage = action.notion_page
    npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
    pb.update_record(collection, action.pb_id, npage_dict | {
        "notion_last_edited": npage.get("last_edited_time"),
        "last_synced_at": now_iso_datetime(),
    })
    write_auto_applied(nc, collection=collection,
                       direction="Notion→PB",
                       summary=str(npage_dict.get(title_field, ""))[:80],
                       pb_id=action.pb_id, notion_id=npage["id"],
                       record_link=npage.get("url"))


def _apply_pb_new(action: PbNew, *,
                  collection: str,
                  notion_db_id: str,
                  field_types: dict,
                  overrides_inv: dict,
                  title_field: str,
                  notion_schema: dict,
                  pb: PBClient, nc: NotionClient) -> None:
    r = action.pb_row
    props = pb_record_to_notion_props(r, field_types, overrides_inv,
                                       title_field, notion_schema)
    props["pb_id"] = {"rich_text": [{"type": "text", "text": {"content": r["id"]}}]}
    props["last_synced_at"] = {"date": {"start": now_iso_date()}}
    page = nc.create_page(notion_db_id, props)
    pb.update_record(collection, r["id"], {
        "notion_id": page["id"],
        "notion_last_edited": page.get("last_edited_time"),
        "last_synced_at": now_iso_datetime(),
    })
    write_auto_applied(nc, collection=collection,
                       direction="PB→Notion",
                       summary=f"new: {str(r.get(title_field, ''))[:60]}",
                       pb_id=r["id"], notion_id=page["id"],
                       record_link=page.get("url"))


def _apply_notion_new(action: NotionNew, *,
                      collection: str,
                      field_types: dict,
                      overrides: dict,
                      title_field: str,
                      pb: PBClient, nc: NotionClient) -> None:
    npage = action.notion_page
    npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
    created = pb.create_record(collection, npage_dict | {
        "notion_id": npage["id"],
        "notion_last_edited": npage.get("last_edited_time"),
        "last_synced_at": now_iso_datetime(),
    })
    nc.update_page(npage["id"], properties={
        "pb_id": {"rich_text": [{"type": "text",
                                  "text": {"content": created["id"]}}]},
        "last_synced_at": {"date": {"start": now_iso_date()}},
    })
    write_auto_applied(nc, collection=collection,
                       direction="Notion→PB",
                       summary=f"new: {str(npage_dict.get(title_field, ''))[:60]}",
                       pb_id=created["id"], notion_id=npage["id"],
                       record_link=npage.get("url"))


def sync_collection(cfg_row: dict, pb: PBClient, nc: NotionClient) -> dict:
    collection = cfg_row["collection"]
    notion_db_id = cfg_row["notion_db_id"]
    overrides = cfg_row.get("field_map_overrides") or {}
    overrides_inv = {v: k for k, v in overrides.items()}
    last_synced_at = cfg_row.get("last_synced_at") or ""

    field_types = collection_field_types(pb, collection)
    title_field = TITLE_FIELD_BY_COLLECTION.get(collection, "title")

    notion_db = nc.retrieve_database(notion_db_id)
    notion_schema = notion_db.get("properties", {})

    pb_rows = pb.list_records(collection, sort="")
    notion_rows = nc.query_database(notion_db_id)
    actions = categorize(pb_rows, notion_rows, last_synced_at=last_synced_at)

    counts: dict[str, int] = {}
    for a in actions:
        counts[type(a).__name__] = counts.get(type(a).__name__, 0) + 1

    for a in actions:
        try:
            if isinstance(a, NoChange):
                continue
            elif isinstance(a, PbOnlyChange):
                _apply_pb_to_notion(a, collection=collection,
                                     field_types=field_types,
                                     overrides_inv=overrides_inv,
                                     title_field=title_field,
                                     notion_schema=notion_schema,
                                     pb=pb, nc=nc)
            elif isinstance(a, NotionOnlyChange):
                _apply_notion_to_pb(a, collection=collection,
                                     field_types=field_types,
                                     overrides=overrides,
                                     title_field=title_field,
                                     pb=pb, nc=nc)
            elif isinstance(a, PbNew):
                _apply_pb_new(a, collection=collection,
                               notion_db_id=notion_db_id,
                               field_types=field_types,
                               overrides_inv=overrides_inv,
                               title_field=title_field,
                               notion_schema=notion_schema,
                               pb=pb, nc=nc)
            elif isinstance(a, NotionNew):
                _apply_notion_new(a, collection=collection,
                                   field_types=field_types,
                                   overrides=overrides,
                                   title_field=title_field,
                                   pb=pb, nc=nc)
            elif isinstance(a, BothChanged):
                pb_id = a.pb_row["id"]
                notion_id = a.notion_page["id"]
                if pending_action_exists(nc, op="Conflict",
                                          pb_id=pb_id, notion_id=notion_id):
                    continue   # already queued from a prior run
                notion_dict = notion_page_to_pb_dict(
                    a.notion_page, field_types, overrides,
                )
                write_conflict(
                    nc,
                    collection=collection,
                    summary=str(a.pb_row.get(title_field, ""))[:120],
                    pb_id=pb_id, notion_id=notion_id,
                    pb_snapshot=a.pb_row,
                    notion_snapshot=notion_dict,
                    record_link=a.notion_page.get("url"),
                )
            elif isinstance(a, NotionVanished):
                pb_id = a.pb_row["id"]
                missing_nid = a.pb_row.get("notion_id") or ""
                if pending_action_exists(nc, op="Delete?",
                                          pb_id=pb_id, notion_id=missing_nid):
                    continue
                write_delete_question(
                    nc,
                    collection=collection,
                    summary=("Notion page missing: "
                             + str(a.pb_row.get(title_field, ""))[:80]),
                    pb_id=pb_id, notion_id=missing_nid,
                    snapshot=a.pb_row,
                )
            elif isinstance(a, PbVanished):
                missing_pid = _pb_id_from_notion(a.notion_page)
                notion_id = a.notion_page["id"]
                if pending_action_exists(nc, op="Delete?",
                                          pb_id=missing_pid, notion_id=notion_id):
                    continue
                notion_dict = notion_page_to_pb_dict(
                    a.notion_page, field_types, overrides,
                )
                write_delete_question(
                    nc,
                    collection=collection,
                    summary=("PB record missing: "
                             + str(notion_dict.get(title_field, ""))[:80]),
                    pb_id=missing_pid, notion_id=notion_id,
                    snapshot=notion_dict,
                )
        except Exception as e:
            log_event("apply_error",
                      collection=collection,
                      action=type(a).__name__,
                      error=str(e),
                      trace=traceback.format_exc()[:1000])

    return {
        "counts": counts,
        "applied": (counts.get("PbOnlyChange", 0)
                    + counts.get("NotionOnlyChange", 0)
                    + counts.get("PbNew", 0)
                    + counts.get("NotionNew", 0)),
        "conflicts": counts.get("BothChanged", 0),
        "deletes": counts.get("NotionVanished", 0) + counts.get("PbVanished", 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-now", action="store_true",
                    help="Bypass the time guard (still respects paused flag)")
    ap.add_argument("--only", help="Restrict to one collection")
    args = ap.parse_args()

    pb = PBClient()
    nc = NotionClient()

    globals_ = pb.list_records("sync_global", sort="")
    sync_global = globals_[0] if globals_ else {}

    if not args.force_now:
        if not should_run_now(sync_global):
            return 0
    elif sync_global.get("paused"):
        log_event("skipped_paused")
        return 0

    log_event("run_start", forced=args.force_now)

    targets = pb.list_records("sync_config", filter="enabled=true", sort="")
    if args.only:
        targets = [t for t in targets if t["collection"] == args.only]
        if not targets:
            log_event("run_aborted", reason=f"no sync_config for {args.only}")
            return 1

    overall: dict[str, int] = {"applied": 0, "conflicts": 0, "deletes": 0}
    for t in targets:
        try:
            result = sync_collection(t, pb, nc)
            for k in ("applied", "conflicts", "deletes"):
                overall[k] += result.get(k, 0)
            pb.update_record("sync_config", t["id"], {
                "last_synced_at": now_iso_datetime(),
                "last_sync_summary": (
                    f"runner: applied={result['applied']} "
                    f"conflicts={result['conflicts']} deletes={result['deletes']}"
                ),
            })
            log_event("collection_done",
                      collection=t["collection"],
                      **result)
        except Exception as e:
            log_event("collection_error",
                      collection=t["collection"],
                      error=str(e),
                      trace=traceback.format_exc()[:2000])

    if sync_global.get("id"):
        pb.update_record("sync_global", sync_global["id"], {
            "last_run_at": now_iso_datetime(),
        })

    log_event("run_end", **overall)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Step 2: Update `notion_sync/__init__.py` docstring

Find:
```python
"""Notion ↔ PocketBase sync package.

PR1 contents: pb_api, notion_api, codec, matching, backup, activity helpers.
PR2 adds: the cron-driven sync runner.
PR3 adds: MCP tools + push notifier.
"""
```

Replace with:
```python
"""Notion ↔ PocketBase sync package.

PR1 contents: pb_api, notion_api, codec, matching, backup, activity,
              transform (shared by reconcile_initial + runner).
PR2 contents: changeset, logger, runner — daily cron sync runner.
PR3 adds: MCP tools + push notifier + Sync Activity decision applier.
"""
```

### Step 3: Verify syntax

```powershell
python -c "import ast; ast.parse(open('notion_sync/runner.py', encoding='utf-8').read()); print('syntax ok')"
```

### Step 4: Commit

```powershell
git add notion_sync/runner.py notion_sync/__init__.py
git commit -m "$(cat <<'EOF'
PR2: notion_sync.runner — daily cron sync runner

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `tests/notion_sync/test_runner_guard.py` — timezone-guard tests

**Files:**
- Create: `tests/notion_sync/test_runner_guard.py`

### Step 1: Write the tests

```python
"""should_run_now tests — pure function with no I/O."""
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.runner import should_run_now


def _utc(year, month, day, hour):
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def test_runs_at_configured_hour_in_local_tz():
    # 07:00 UTC == 03:00 America/New_York (EDT, summer)
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7)) is True


def test_does_not_run_off_hour():
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 8)) is False
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 6)) is False


def test_respects_paused():
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3, "paused": True}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7)) is False


def test_handles_tokyo():
    # 18:00 UTC == 03:00 Asia/Tokyo (JST = UTC+9)
    cfg = {"timezone": "Asia/Tokyo", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 18)) is True
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 19)) is False


def test_handles_missing_config_safely():
    # Defaults: UTC + sync_hour_local=3
    cfg = {}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 3)) is True
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 4)) is False


def test_bad_timezone_returns_false():
    cfg = {"timezone": "Mars/Olympus", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 6, 1, 7)) is False


def test_daylight_savings_us_winter():
    # In winter, America/New_York is EST = UTC-5, so 03:00 local == 08:00 UTC
    cfg = {"timezone": "America/New_York", "sync_hour_local": 3, "paused": False}
    assert should_run_now(cfg, now_utc=_utc(2026, 1, 15, 8)) is True
    assert should_run_now(cfg, now_utc=_utc(2026, 1, 15, 7)) is False
```

### Step 2: Verify

```powershell
python -m pytest tests/notion_sync/test_runner_guard.py -v
```
Expected: 7 tests pass.

### Step 3: Commit

```powershell
git add tests/notion_sync/test_runner_guard.py
git commit -m "$(cat <<'EOF'
PR2: tests for should_run_now timezone guard

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: systemd unit files + installer

**Files:**
- Create: `deploy/notion-sync.service`
- Create: `deploy/notion-sync.timer`
- Create: `deploy/install_systemd.sh`

### Step 1: `deploy/notion-sync.service`

```ini
[Unit]
Description=Notion <-> PB sync runner (hourly check, runs only at configured local hour)
After=network.target pocketbase.service

[Service]
Type=oneshot
User=dev
Group=dev
WorkingDirectory=/home/dev/phone-bridge
EnvironmentFile=/home/dev/phone-bridge/.env
ExecStart=/home/dev/phone-bridge/.venv/bin/python -m notion_sync.runner
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Step 2: `deploy/notion-sync.timer`

```ini
[Unit]
Description=Hourly trigger for notion-sync.service

[Timer]
OnCalendar=hourly
Persistent=true
Unit=notion-sync.service

[Install]
WantedBy=timers.target
```

### Step 3: `deploy/install_systemd.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

sudo install -m 644 notion-sync.service /etc/systemd/system/notion-sync.service
sudo install -m 644 notion-sync.timer   /etc/systemd/system/notion-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now notion-sync.timer

echo
echo "Installed. Status:"
systemctl status notion-sync.timer --no-pager
echo
echo "Next 3 wake-ups:"
systemctl list-timers notion-sync.timer --no-pager
```

### Step 4: Commit

```powershell
git add deploy/notion-sync.service deploy/notion-sync.timer deploy/install_systemd.sh
git commit -m "$(cat <<'EOF'
PR2: systemd service + hourly timer + idempotent installer

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

### Step 1: Replace the existing "Notion sync (PR1 baseline)" section

Find the section header `## Notion sync (PR1 baseline)` and replace it through to (but not including) `## Architecture` with:

```markdown
## Notion sync

PR1 wired the schema and the initial PB ↔ Notion alignment for 6 collections
(trips/days/plans/todos/contacts/locations). PR2 added the daily auto-sync
runner. PR3 (not done) will add the Sync Activity decision applier and push
notifications.

**Daily operation:**
- systemd timer `notion-sync.timer` fires hourly.
- The runner reads `sync_global.timezone` + `sync_hour_local` and exits
  silently unless the current hour in that timezone equals the configured
  sync hour.
- When it does run: for each enabled `sync_config` row it categorizes
  rows into changed-one-side / changed-both / new / vanished, applies
  the no-conflict ones, and writes one `Auto-applied` row to Sync Activity
  per change. **Conflicts and vanishings are enqueued to Sync Activity
  with `decision=Pending`** so you can review snapshots in Notion and pick
  a winner. Re-detected conflicts/deletes don't duplicate-write (idempotent).
- PR2 does **not** auto-apply user decisions yet — that lands in PR3. You
  can still mark decisions in Notion; PR3 will pick them up on first run.
- `sync_config[*].last_sync_summary` reflects the latest pass.

**Force a run now:**
```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -m notion_sync.runner --force-now              # all enabled
.venv/bin/python -m notion_sync.runner --force-now --only trips # one table
```

**Pause:** set `sync_global.paused = true` via PB admin or REST. The next
hourly tick logs `skipped_paused` and exits without touching anything.

**Logs:**
- operational events JSON lines: `/home/dev/phone-bridge/.bridge_data/sync.log`
  (run_start, run_end, apply_error, skipped_paused, bad_timezone)
- conflicts/deletes: NOT in the log file — go to Notion Sync Activity DB
- systemd journal: `journalctl -u notion-sync.service -f`

**Change the schedule / timezone:** update `sync_global` in PB. Takes
effect at the next hourly tick — no systemctl reload needed.

**Re-running initial reconcile** (still available):
```bash
.venv/bin/python scripts/reconcile_initial.py --only <collection> --dry-run
.venv/bin/python scripts/reconcile_initial.py --only <collection>
```
```

### Step 2: Commit

```powershell
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
PR2: document runner / logs / pause / timezone in CLAUDE.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Live deploy + verify (controller-driven, not subagent)

This task happens after the user approves PR2 code. NOT for subagent — controller runs with user present.

### Step 1: Deploy

```powershell
deploy
```

The `deploy/` folder ships to the VM at `/home/dev/phone-bridge/deploy/` (it's not in `.deploy.json` excludes).

### Step 2: Install systemd units (idempotent)

```powershell
ssh dashboard-server "cd /home/dev/phone-bridge/deploy && bash install_systemd.sh"
```

Expected output ends with `notion-sync.timer ... active (waiting)` and the next 3 wake-up times.

### Step 3: Smoke test — `--force-now` against a clean state

PR1 + the test todo are already synced; there should be nothing pending. Run:

```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python -m notion_sync.runner --force-now --only locations"
```

Expected: exit 0, no stdout, and `.bridge_data/sync.log` gets `run_start` + `collection_done` (applied=0) + `run_end`. Verify:

```powershell
ssh dashboard-server "tail -10 /home/dev/phone-bridge/.bridge_data/sync.log"
```

### Step 4: Round-trip tests

**Test A — PB → Notion auto-apply:**
- In Phone Bridge, ask Claude: "Add a todo: PR2 test todo, due tomorrow"
- Force-run: `.venv/bin/python -m notion_sync.runner --force-now --only todos`
- Verify in Notion: the todo appears as a new page (linked, with `pb_id` filled)
- Verify in Sync Activity: a new row with `op=Auto-applied`, `direction=PB→Notion`

**Test B — Notion → PB auto-apply:**
- In Notion, edit any todo's title (append `(edited)`)
- Wait 5 seconds (so `last_edited_time` advances)
- Force-run: `.venv/bin/python -m notion_sync.runner --force-now --only todos`
- Verify in PB admin: the todo's title now ends with `(edited)`
- Verify in Sync Activity: a new row with `op=Auto-applied`, `direction=Notion→PB`

**Test C — vanished detection (enqueue + idempotent):**
- Pick a recently-created Notion todo and archive it via Notion's `…` → Delete from sidebar. Archived pages don't appear in `query_database`.
- Force-run.
- Verify Sync Activity got a new row with `op=Delete?`, `decision=Pending`, summary "Notion page missing: ...", pb_snapshot filled.
- Verify PB still has the row (PR2 detects + queues, doesn't auto-delete).
- Force-run again — Sync Activity should still have exactly **one** Delete? row (idempotent skip via `pending_action_exists`).

**Test D — conflict detection (enqueue + idempotent):**
- Pick a linked todo. Edit its title in PB admin (e.g. append " [pb-edit]"). Then edit the SAME todo's title in Notion (append " [notion-edit]"). Wait 5s for Notion's last_edited_time to settle.
- Force-run.
- Verify Sync Activity got a new row with `op=Conflict`, `decision=Pending`, both pb_snapshot and notion_snapshot filled.
- Verify NEITHER side was overwritten (PB still has " [pb-edit]", Notion still has " [notion-edit]").
- Force-run again — still one Conflict row.
- (Don't resolve the conflict yet — PR3 needs Pending input to test against.)

### Step 5: Wait for natural cron tick

If the configured time is `America/New_York` 03:00 and we're in the afternoon ET, the next real run is at 03:00 ET. After it fires, check:

```powershell
ssh dashboard-server "tail -40 /home/dev/phone-bridge/.bridge_data/sync.log; echo --; systemctl status notion-sync.timer --no-pager"
```

Expected: `run_start` + per-collection `collection_done` + `run_end` lines from 03:00 local; timer status shows "last triggered" at the right time.

---

## Self-Review Checklist

After completing all tasks, verify:

- [ ] `python -m pytest tests/notion_sync/ -v` — all green (10 new changeset tests + 7 new guard tests + 41 existing PR1 tests = 58 total).
- [ ] `systemctl is-active notion-sync.timer` returns `active` on the VM.
- [ ] `.bridge_data/sync.log` exists and gets new lines after each `--force-now`.
- [ ] At least one `op=Auto-applied` row appears in the Notion Sync Activity DB after Test A.
- [ ] One `op=Conflict` row and one `op=Delete?` row appear after Tests C and D (both `decision=Pending`).
- [ ] Re-running --force-now doesn't duplicate the Conflict / Delete? rows (idempotent check).
- [ ] `sync_config[trips].last_sync_summary` has been written by the runner.
- [ ] No phantom records — PB row count + Notion page count match PR1 end-state plus exactly the test edits.

---

## Out of scope (deferred to PR3)

- Sync Activity decision applier — read user-set `decision != Pending` rows on next run, apply the chosen side, fill `applied_at`.
- Push notification when Pending > 0.
- MCP tools `sync_now`, `sync_queue_status`, `sync_pause`.
- 30-/90-day auto-cleanup of resolved Sync Activity rows.
- Field-level conflict detail (PR2 marks the whole row as "BothChanged" with full snapshots; PR3 can refine summary to per-field).
