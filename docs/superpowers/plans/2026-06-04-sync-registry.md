# Sync Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three hardcoded Python maps that decide "what gets synced to Notion" with a runtime-editable PB-backed registry, plus a settings UI that can register a new sync target end-to-end (including auto-creating the Notion DB).

**Architecture:** Extend the existing `sync_config` PB table with `title_field` / `date_field` / `auto_sync` columns (migration `1779465623`). Add a thin loader module `notion_sync/config.py` with a 60s in-process cache. Refactor `runner.py`, `reconcile_initial.py`, `pb_tools.py` to consult the loader. Add `notion_sync/provisioner.py` that creates a Notion DB matching a PB collection's schema. Wire 5 new REST endpoints in `server.py` and extend the existing 同步设置 modal in `static/`. Add a manual snapshot dumper (`scripts/dump_sync_registry.py`) and YAML output committed to git.

**Tech Stack:** Python 3.11 stdlib (urllib, dataclasses), PocketBase JS migrations, FastAPI, vanilla JS, no new pip deps.

**Spec:** [`docs/sync-registry-design.md`](../../sync-registry-design.md) — every section reference like "§3.3" below points there.

---

## File map

| Action | Path |
|---|---|
| **Create** | `pocketbase/pb_migrations/1779465623_extend_sync_config.js` |
| **Create** | `notion_sync/config.py` |
| **Create** | `notion_sync/provisioner.py` |
| **Create** | `scripts/dump_sync_registry.py` |
| **Create** | `notion_sync/registry.snapshot.yaml` (generated, committed) |
| **Create** | `tests/notion_sync/test_config.py` |
| **Create** | `tests/notion_sync/test_provisioner.py` |
| **Modify** | `notion_sync/runner.py` (drop hardcoded dict, read from `cfg_row`) |
| **Modify** | `scripts/reconcile_initial.py` (drop two dicts, accept fields as params) |
| **Modify** | `pb_tools.py` (drop hardcoded set, call `collections_with_auto_sync()`) |
| **Modify** | `server.py` (5 new endpoints) |
| **Modify** | `static/index.html` (add section + sub-dialog) |
| **Modify** | `static/app.js` (loaders + handlers) |
| **Modify** | `static/style.css` (minor) |
| **Modify** | `tests/notion_sync/test_runner_guard.py` (fixture: add `title_field` if needed) |
| **Modify** | `CLAUDE.md`, `docs/notion-pb-sync.md`, `docs/data-model.md`, `scripts/setup_notion_sync_db.py` (docs) |

---

## Task 1: PB migration — extend sync_config schema

**Files:**
- Create: `pocketbase/pb_migrations/1779465623_extend_sync_config.js`

**References:** Spec §3.

- [ ] **Step 1.1: Create the migration file**

Path: `pocketbase/pb_migrations/1779465623_extend_sync_config.js`. The filename timestamp **must** be `1779465623` (next after `1779465622_add_second_sync_hour.js`). Contents verbatim:

```js
/// <reference path="../pb_data/types.d.ts" />
//
// Extend sync_config with the three columns that previously lived as
// hardcoded Python dicts. After this migration, runner.py /
// reconcile_initial.py / pb_tools.py read these values from the
// sync_config rows instead of their local maps.
//
// Idempotent: skips each field if already present; seeds existing rows
// only when the new column is null / empty.
//
migrate((app) => {
  const c = app.findCollectionByNameOrId("sync_config");

  if (!c.fields.getByName("title_field")) {
    c.fields.add(new Field({ name: "title_field", type: "text", required: true, max: 60 }));
    app.save(c);
  }
  if (!c.fields.getByName("date_field")) {
    c.fields.add(new Field({ name: "date_field", type: "text", max: 60 }));
    app.save(c);
  }
  if (!c.fields.getByName("auto_sync")) {
    c.fields.add(new Field({ name: "auto_sync", type: "bool" }));
    app.save(c);
  }

  const SEED = {
    trips:     { title_field: "title", date_field: "date_start",  auto_sync: true  },
    plans:     { title_field: "title", date_field: "target_date", auto_sync: false },
    todos:     { title_field: "title", date_field: "due_date",    auto_sync: true  },
    journal:   { title_field: "title", date_field: "date",        auto_sync: true  },
    days:      { title_field: "name",  date_field: "date",        auto_sync: true  },
    contacts:  { title_field: "name",  date_field: "",            auto_sync: false },
    locations: { title_field: "name",  date_field: "",            auto_sync: true  },
    stops:     { title_field: "name",  date_field: "date",        auto_sync: true  },
  };
  const rows = app.findRecordsByFilter("sync_config", "");
  for (const row of rows) {
    const seed = SEED[row.get("collection")];
    if (!seed) continue;
    let dirty = false;
    if (!row.get("title_field")) { row.set("title_field", seed.title_field); dirty = true; }
    if (row.get("date_field") == null || row.get("date_field") === "") {
      row.set("date_field", seed.date_field); dirty = true;
    }
    if (row.get("auto_sync") === null || row.get("auto_sync") === undefined) {
      row.set("auto_sync", seed.auto_sync); dirty = true;
    }
    if (dirty) app.save(row);
  }
}, (app) => {
  const c = app.findCollectionByNameOrId("sync_config");
  for (const name of ["title_field", "date_field", "auto_sync"]) {
    const f = c.fields.getByName(name);
    if (f) { c.fields.removeById(f.id); app.save(c); }
  }
});
```

- [ ] **Step 1.2: Commit the migration**

```bash
git add pocketbase/pb_migrations/1779465623_extend_sync_config.js
git commit -m "PB migration: extend sync_config with title_field / date_field / auto_sync"
```

- [ ] **Step 1.3: Deploy to the VM**

Run the `deploy` tool (no args — `.deploy.json` is configured). Per `CLAUDE.md`, this restarts `phone-bridge.service` and copies `pocketbase/pb_migrations/*.js` to `/opt/pocketbase/pb_migrations/`. PB auto-applies new migrations on next start.

- [ ] **Step 1.4: Verify schema + seed via REST**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && set -a && . ./.env && set +a && \
  curl -sS -H "Authorization: $PB_TOKEN" "$PB_URL/api/collections/sync_config/records?perPage=20" \
    | python3 -m json.tool | head -80'
```

Expected: each of the 8 rows (trips, plans, todos, journal, days, contacts, locations, stops) has `title_field`, `date_field`, `auto_sync` populated per §3.3 SEED dict. Confirm `trips` has `auto_sync: true` and `plans` has `auto_sync: false`.

---

## Task 2: notion_sync/config.py — registry loader (TDD)

**Files:**
- Create: `notion_sync/config.py`
- Create: `tests/notion_sync/test_config.py`

**References:** Spec §4.

- [ ] **Step 2.1: Write the failing tests**

Path: `tests/notion_sync/test_config.py`. Contents:

```python
"""Unit tests for the sync registry loader."""
import notion_sync.config as cfg


class FakePB:
    def __init__(self, rows): self.rows = rows
    def list_records(self, *_, **__): return list(self.rows)


def test_load_all_projects_rows():
    pb = FakePB([{
        "id": "r1", "collection": "trips",
        "notion_db_id": "db1", "enabled": True, "auto_sync": True,
        "title_field": "title", "date_field": "date_start",
        "field_map_overrides": {"foo": "Foo"},
        "last_synced_at": "2026-06-04 03:00:00.000Z",
        "last_sync_summary": "",
    }])
    cfg.invalidate()
    targets = cfg.load_all(pb, fresh=True)
    assert len(targets) == 1
    t = targets[0]
    assert t.collection == "trips"
    assert t.title_field == "title"
    assert t.overrides_inverse == {"Foo": "foo"}


def test_collections_with_auto_sync_filters_correctly():
    pb = FakePB([
        {"id": "1", "collection": "trips",   "enabled": True,  "auto_sync": True,  "title_field": "title"},
        {"id": "2", "collection": "plans",   "enabled": True,  "auto_sync": False, "title_field": "title"},
        {"id": "3", "collection": "contacts","enabled": False, "auto_sync": True,  "title_field": "name"},
    ])
    cfg.invalidate()
    assert cfg.collections_with_auto_sync(pb, fresh=True) == {"trips"}


def test_cache_returns_same_list_within_ttl():
    pb = FakePB([{"id": "1", "collection": "trips", "enabled": True,
                   "auto_sync": True, "title_field": "title"}])
    cfg.invalidate()
    a = cfg.load_all(pb)
    pb.rows = []                               # mutate underlying source
    b = cfg.load_all(pb)                       # still cached
    assert a[0].collection == b[0].collection


def test_invalidate_clears_cache():
    pb = FakePB([{"id": "1", "collection": "trips", "enabled": True,
                   "auto_sync": True, "title_field": "title"}])
    cfg.invalidate()
    _ = cfg.load_all(pb)
    pb.rows = []
    cfg.invalidate()
    assert cfg.load_all(pb) == []


def test_get_returns_none_for_unknown():
    pb = FakePB([])
    cfg.invalidate()
    assert cfg.get("nope", pb, fresh=True) is None
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `python -m pytest tests/notion_sync/test_config.py -v`
Expected: `ModuleNotFoundError: notion_sync.config` (module doesn't exist yet).

- [ ] **Step 2.3: Create the loader module**

Path: `notion_sync/config.py`. Contents verbatim from spec §4.2:

```python
"""Sync registry — single read path for all per-collection sync metadata.

Backed by the PB `sync_config` table (one row per synced collection).
Other modules (runner, reconcile, pb_tools) MUST go through this loader
instead of caching their own dicts.

Cache: in-process 60s TTL so per-tool-call lookups don't hammer PB. The
cache is module-level, shared across import sites within one process.
`invalidate()` clears it (called by the REST handlers that mutate
sync_config).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from notion_sync.pb_api import PBClient


@dataclass(frozen=True)
class SyncTarget:
    """One row of sync_config, projected into a typed shape."""
    id: str
    collection: str
    notion_db_id: str
    enabled: bool
    auto_sync: bool
    title_field: str
    date_field: str
    field_map_overrides: dict[str, str]
    last_synced_at: str
    last_sync_summary: str

    @property
    def overrides_inverse(self) -> dict[str, str]:
        return {v: k for k, v in self.field_map_overrides.items()}


_CACHE_TTL_SECONDS = 60.0
_cache: tuple[float, list[SyncTarget]] | None = None


def _row_to_target(row: dict) -> SyncTarget:
    return SyncTarget(
        id=row["id"],
        collection=row["collection"],
        notion_db_id=row.get("notion_db_id") or "",
        enabled=bool(row.get("enabled")),
        auto_sync=bool(row.get("auto_sync")),
        title_field=row.get("title_field") or "",
        date_field=row.get("date_field") or "",
        field_map_overrides=row.get("field_map_overrides") or {},
        last_synced_at=row.get("last_synced_at") or "",
        last_sync_summary=row.get("last_sync_summary") or "",
    )


def load_all(pb: PBClient | None = None, *, fresh: bool = False) -> list[SyncTarget]:
    global _cache
    now = time.monotonic()
    if not fresh and _cache and (now - _cache[0]) < _CACHE_TTL_SECONDS:
        return list(_cache[1])
    pb = pb or PBClient()
    rows = pb.list_records("sync_config", sort="")
    targets = [_row_to_target(r) for r in rows]
    _cache = (now, targets)
    return list(targets)


def load_enabled(pb: PBClient | None = None, *, fresh: bool = False) -> list[SyncTarget]:
    return [t for t in load_all(pb, fresh=fresh) if t.enabled]


def get(collection: str, pb: PBClient | None = None,
        *, fresh: bool = False) -> SyncTarget | None:
    for t in load_all(pb, fresh=fresh):
        if t.collection == collection:
            return t
    return None


def collections_with_auto_sync(pb: PBClient | None = None,
                                *, fresh: bool = False) -> set[str]:
    return {t.collection for t in load_enabled(pb, fresh=fresh) if t.auto_sync}


def invalidate() -> None:
    global _cache
    _cache = None
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `python -m pytest tests/notion_sync/test_config.py -v`
Expected: 5 passed.

- [ ] **Step 2.5: Commit**

```bash
git add notion_sync/config.py tests/notion_sync/test_config.py
git commit -m "notion_sync/config.py: PB-backed registry loader with 60s cache"
```

---

## Task 3: Refactor runner.py to read title_field from the row

**Files:**
- Modify: `notion_sync/runner.py`
- Modify: `tests/notion_sync/test_runner_guard.py` (fixtures, only if needed)

**References:** Spec §6.1.

- [ ] **Step 3.1: Delete the hardcoded dict**

In `notion_sync/runner.py`, delete lines 55–58 — the entire `TITLE_FIELD_BY_COLLECTION = {...}` block including the blank line above and below it. Verify nothing else in the file references that name:

```bash
grep -n "TITLE_FIELD_BY_COLLECTION" notion_sync/runner.py
```
Expected: no matches after the edit.

- [ ] **Step 3.2: Replace the lookup inside `sync_collection`**

Inside `sync_collection` (currently ~line 353), find:

```python
    title_field = TITLE_FIELD_BY_COLLECTION.get(collection, "title")
```

Replace with:

```python
    title_field = cfg_row.get("title_field") or ""
    if not title_field:
        raise RuntimeError(
            f"sync_config[{collection}].title_field is empty — set it via "
            f"the settings UI or PB admin before this collection can sync"
        )
```

- [ ] **Step 3.3: Check existing test fixtures**

```bash
grep -n "collection.*:" tests/notion_sync/test_runner_guard.py
```

If any test passes a `sync_config`-shaped dict into `sync_collection`, add `"title_field": "title"` to it. The existing `test_runner_guard.py` only tests the time-guard pure function (does NOT call `sync_collection`), so it likely needs no edits — verify with the grep then move on.

- [ ] **Step 3.4: Run tests**

Run: `python -m pytest tests/notion_sync/ -v`
Expected: all tests pass.

- [ ] **Step 3.5: Commit**

```bash
git add notion_sync/runner.py tests/notion_sync/test_runner_guard.py
git commit -m "runner.py: drop TITLE_FIELD_BY_COLLECTION, read title_field from sync_config row"
```

- [ ] **Step 3.6: Deploy + smoke-test**

Run `deploy`. Then on the VM:

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && set -a && . ./.env && set +a && \
  .venv/bin/python -m notion_sync.runner --force-now --only trips'
```

Expected: exit code 0, no NameError. Then:

```bash
ssh dashboard-server 'tail -5 /home/dev/phone-bridge/.bridge_data/sync.log'
```

Expected: recent `collection_done` for `trips`.

---

## Task 4: Refactor reconcile_initial.py

**Files:**
- Modify: `scripts/reconcile_initial.py`

**References:** Spec §6.2.

- [ ] **Step 4.1: Delete the two hardcoded dicts**

In `scripts/reconcile_initial.py`, delete lines 42–51 (both `TITLE_FIELD_BY_COLLECTION` and `DATE_FIELD_BY_COLLECTION` blocks). Verify:

```bash
grep -nE "TITLE_FIELD_BY_COLLECTION|DATE_FIELD_BY_COLLECTION" scripts/reconcile_initial.py
```
Expected: no matches.

- [ ] **Step 4.2: Change `reconcile_one` signature + body**

Find:

```python
def reconcile_one(collection: str, notion_db_id: str,
                  overrides: dict[str, str],
                  pb: PBClient, nc: NotionClient,
                  dry_run: bool) -> dict:
```

Replace with:

```python
def reconcile_one(collection: str, notion_db_id: str,
                  overrides: dict[str, str],
                  title_field: str,
                  date_field: str,
                  pb: PBClient, nc: NotionClient,
                  dry_run: bool) -> dict:
```

Then inside the function body, delete the two local lookups (the lines that read `title_field = TITLE_FIELD_BY_COLLECTION.get(...)` and `date_field = DATE_FIELD_BY_COLLECTION.get(...)`). The parameters now carry them.

- [ ] **Step 4.3: Update the call site in `main()`**

Find the `for t in targets:` loop and the `reconcile_one(...)` call. Replace it with:

```python
        for t in targets:
            try:
                result = reconcile_one(
                    collection=t["collection"],
                    notion_db_id=t["notion_db_id"],
                    overrides=t.get("field_map_overrides") or {},
                    title_field=t.get("title_field") or "title",
                    date_field=t.get("date_field") or "",
                    pb=pb, nc=nc,
                    dry_run=args.dry_run,
                )
```

- [ ] **Step 4.4: Dry-run smoke test against the VM**

Run `deploy` first, then on the VM:

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && set -a && . ./.env && set +a && \
  .venv/bin/python scripts/reconcile_initial.py --only trips --dry-run'
```

Expected: prints `=== trips ===` plus a non-error summary. No NameError.

- [ ] **Step 4.5: Commit**

```bash
git add scripts/reconcile_initial.py
git commit -m "reconcile_initial.py: read title_field / date_field from sync_config row"
```

---

## Task 5: Refactor pb_tools.py auto-sync set

**Files:**
- Modify: `pb_tools.py`

**References:** Spec §6.3.

- [ ] **Step 5.1: Delete the hardcoded set**

In `pb_tools.py`, find lines 51–53:

```python
_AUTO_SYNC_COLLECTIONS: set[str] = {
    "trips", "days", "stops", "locations", "todos", "journal",
}
```

Delete those three lines AND the 3-line comment block immediately above them (the one starting with `# Collections that auto-trigger sync.`). Verify:

```bash
grep -nE "_AUTO_SYNC_COLLECTIONS" pb_tools.py
```
Expected: no matches.

- [ ] **Step 5.2: Add the import**

At the top of `pb_tools.py`, after `from claude_agent_sdk import create_sdk_mcp_server, tool`, add:

```python
from notion_sync.config import collections_with_auto_sync
```

- [ ] **Step 5.3: Replace `_schedule_auto_sync`**

Find `def _schedule_auto_sync(collection: str) -> None:` and replace its body with:

```python
def _schedule_auto_sync(collection: str) -> None:
    """Add a collection to the pending set and (re-)arm the debounced runner.

    Whether a collection auto-syncs is now driven by sync_config rows
    (auto_sync=true + enabled=true). The set is cached for 60s by the
    loader so this is not a per-write PB hit in steady state.
    """
    try:
        auto = collections_with_auto_sync()
    except Exception as e:
        log.warning("auto-sync registry unavailable: %s", e)
        return
    if collection not in auto:
        return
    _pending_sync.add(collection)
    global _sync_task
    if _sync_task and not _sync_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _sync_task = loop.create_task(_run_debounced_sync())
```

- [ ] **Step 5.4: Update the PROMPT_HINT**

In the `PROMPT_HINT = (...)` block (around lines 649–672), find:

```
"AUTO-SYNC: pb_create / pb_update / pb_delete on trips / days / stops / "
"locations / todos / journal automatically schedules a debounced sync "
```

Replace those two lines with:

```python
    "AUTO-SYNC: pb_create / pb_update / pb_delete on collections marked "
    "auto_sync in sync_config (currently trips / days / stops / locations / "
    "todos / journal — visible in the 同步设置 page) automatically schedules "
    "a debounced sync "
```

(Keep the rest of the paragraph after that intact — the part about "10s window" etc.)

- [ ] **Step 5.5: Deploy**

Run `deploy`.

- [ ] **Step 5.6: Verify trips still auto-syncs**

In a Phone Bridge chat session, ask Claude:

> "Find one trip via mcp__pb__pb_search and update its `note` field to the current timestamp."

Wait ~15 seconds. Then:

```bash
ssh dashboard-server 'tail -10 /home/dev/phone-bridge/.bridge_data/sync.log | grep trips'
```

Expected: a recent `collection_done` event for `trips`.

- [ ] **Step 5.7: Verify plans does NOT auto-sync**

Confirm in 同步设置 (UI) or via REST that `plans.auto_sync = false`:

```bash
ssh dashboard-server 'curl -sS -H "Authorization: $PB_TOKEN" \
  "$PB_URL/api/collections/sync_config/records?filter=(collection=%27plans%27)" \
  | python3 -m json.tool | grep auto_sync'
```

Expected: `"auto_sync": false`. Then in chat:

> "Find one plan via pb_search and update its `note` to the current timestamp."

Wait 30 seconds. Check the log — there should be NO new `collection_done` for `plans`.

- [ ] **Step 5.8: Commit**

```bash
git add pb_tools.py
git commit -m "pb_tools.py: drop hardcoded _AUTO_SYNC_COLLECTIONS, consult sync_config"
```

---

## Task 6: notion_sync/provisioner.py + tests (TDD)

**Files:**
- Create: `notion_sync/provisioner.py`
- Create: `tests/notion_sync/test_provisioner.py`

**References:** Spec §5.

- [ ] **Step 6.1: Write the failing tests**

Path: `tests/notion_sync/test_provisioner.py`. Contents:

```python
"""Tests for notion_sync.provisioner (no real PB / Notion calls)."""
import pytest
from notion_sync import provisioner


class FakePB:
    """Stand-in for PBClient.

    `_http` returns the canned collection dict; `list_records` is used by
    notion_sync.config.load_all (we pre-populate sync_config rows).
    """
    def __init__(self, collections, sync_config_rows):
        self.collections = collections           # name -> coll dict
        self.sync_rows = sync_config_rows

    def list_records(self, name, *_, **__):
        if name == "sync_config":
            return list(self.sync_rows)
        return []

    def _http(self, method, path, body=None):    # noqa: ARG002
        if method == "GET" and path.startswith("/api/collections/"):
            name_or_id = path.rsplit("/", 1)[-1]
            for c in self.collections.values():
                if c["name"] == name_or_id or c["id"] == name_or_id:
                    return c
            raise RuntimeError(f"collection not found: {name_or_id}")
        raise NotImplementedError(method, path)


class FakeNotion:
    def __init__(self):
        self.created_dbs = []
        self.patched_dbs = []
        self.activity_db = {
            "properties": {"collection": {"select": {"options": [
                {"name": "trips"},
            ]}}}
        }
    def create_database(self, parent_page_id, title, properties):
        db = {"id": "new-db-uuid", "title": title, "properties": properties}
        self.created_dbs.append(db)
        return db
    def retrieve_database(self, db_id):
        return self.activity_db
    def update_database(self, db_id, body):
        self.patched_dbs.append((db_id, body))
        new_opts = body["properties"]["collection"]["select"]["options"]
        self.activity_db["properties"]["collection"]["select"]["options"] = new_opts
        return self.activity_db


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("NOTION_SYNC_PARENT_PAGE_ID", "parent-uuid")
    monkeypatch.setenv("NOTION_SYNC_ACTIVITY_DB_ID", "activity-uuid")
    import notion_sync.config as cfg
    cfg.invalidate()


def _coll(name, fields):
    return {"id": f"{name}-id", "name": name, "fields": fields}


def test_basic_text_collection_creates_title_plus_richtext():
    coll = _coll("ideas", [
        {"name": "title", "type": "text", "required": True},
        {"name": "summary", "type": "editor"},
        {"name": "url", "type": "url"},
    ])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    new_id = provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    assert new_id == "new-db-uuid"
    props = nc.created_dbs[0]["properties"]
    assert props["Title"] == {"title": {}}
    assert props["Summary"] == {"rich_text": {}}
    assert props["Url"] == {"url": {}}
    assert props["pb_id"] == {"rich_text": {}}
    assert props["last_synced_at"] == {"date": {}}


def test_select_field_maxselect_1_becomes_select():
    coll = _coll("ideas", [
        {"name": "title", "type": "text"},
        {"name": "status", "type": "select", "maxSelect": 1,
         "values": ["Open", "Done"]},
    ])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    props = nc.created_dbs[0]["properties"]
    assert props["Status"] == {"select": {"options": [
        {"name": "Open"}, {"name": "Done"},
    ]}}


def test_select_field_maxselect_3_becomes_multi_select():
    coll = _coll("ideas", [
        {"name": "title", "type": "text"},
        {"name": "tags", "type": "select", "maxSelect": 3,
         "values": ["a", "b", "c"]},
    ])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    props = nc.created_dbs[0]["properties"]
    assert props["Tags"]["multi_select"]["options"] == [
        {"name": "a"}, {"name": "b"}, {"name": "c"},
    ]


def test_relation_to_synced_target_becomes_relation():
    days_coll = _coll("days", [{"name": "name", "type": "text"}])
    stops_coll = _coll("stops", [
        {"name": "name", "type": "text"},
        {"name": "day", "type": "relation",
         "collectionId": "days-id", "maxSelect": 1},
    ])
    pb = FakePB({"days": days_coll, "stops": stops_coll}, [
        {"id": "1", "collection": "days", "notion_db_id": "days-notion-uuid",
         "enabled": True, "auto_sync": True, "title_field": "name"},
    ])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="stops", title_field="name",
    )
    props = nc.created_dbs[0]["properties"]
    assert props["Day"] == {
        "relation": {"database_id": "days-notion-uuid", "single_property": {}},
    }


def test_relation_to_unsynced_target_is_skipped():
    days_coll = _coll("days", [{"name": "name", "type": "text"}])
    stops_coll = _coll("stops", [
        {"name": "name", "type": "text"},
        {"name": "day", "type": "relation",
         "collectionId": "days-id", "maxSelect": 1},
    ])
    pb = FakePB({"days": days_coll, "stops": stops_coll}, [])   # no sync_config
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="stops", title_field="name",
    )
    props = nc.created_dbs[0]["properties"]
    assert "Day" not in props


def test_password_field_is_skipped():
    coll = _coll("users", [
        {"name": "name", "type": "text"},
        {"name": "password", "type": "password"},
    ])
    pb = FakePB({"users": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="users", title_field="name",
    )
    props = nc.created_dbs[0]["properties"]
    assert "Password" not in props


def test_unknown_title_field_raises():
    coll = _coll("ideas", [{"name": "title", "type": "text"}])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    with pytest.raises(RuntimeError, match="not a field"):
        provisioner.provision_notion_db(
            pb=pb, nc=nc, collection="ideas", title_field="nope",
        )


def test_missing_collection_raises():
    pb = FakePB({}, [])
    nc = FakeNotion()
    with pytest.raises(RuntimeError, match="not found"):
        provisioner.provision_notion_db(
            pb=pb, nc=nc, collection="nope", title_field="title",
        )


def test_sync_activity_option_appended():
    coll = _coll("ideas", [{"name": "title", "type": "text"}])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    assert len(nc.patched_dbs) == 1
    _, patch = nc.patched_dbs[0]
    names = [o["name"] for o in patch["properties"]["collection"]["select"]["options"]]
    assert "ideas" in names
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run: `python -m pytest tests/notion_sync/test_provisioner.py -v`
Expected: `ModuleNotFoundError` on `notion_sync.provisioner`.

- [ ] **Step 6.3: Create the provisioner module**

Path: `notion_sync/provisioner.py`:

```python
"""Auto-provision a Notion database to match a PB collection.

Used when the user enables sync for a previously-not-synced collection
from the settings UI. The created DB includes pb_id + last_synced_at
pipeline columns and the right Notion property type for every PB field.
"""
from __future__ import annotations

import os

from notion_sync.codec import snake_to_title
from notion_sync.config import load_all
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient


_SYSTEM_FIELD_NAMES = {
    "id", "created", "updated",
    "notion_id", "notion_last_edited", "last_synced_at", "pb_id",
}


def provision_notion_db(
    *,
    pb: PBClient,
    nc: NotionClient,
    collection: str,
    title_field: str,
    db_title: str | None = None,
    parent_page_id: str | None = None,
) -> str:
    """Create a Notion database mirroring the PB collection schema."""
    parent_page_id = parent_page_id or os.environ.get("NOTION_SYNC_PARENT_PAGE_ID", "")
    if not parent_page_id:
        raise RuntimeError("NOTION_SYNC_PARENT_PAGE_ID not set")

    coll = _get_collection(pb, collection)
    fields = coll["fields"]
    field_by_name = {f["name"]: f for f in fields}
    if title_field not in field_by_name:
        raise RuntimeError(
            f"title_field={title_field!r} is not a field on PB "
            f"collection {collection!r}. Fields: {sorted(field_by_name)}"
        )

    properties: dict[str, dict] = {snake_to_title(title_field): {"title": {}}}
    targets = load_all(pb, fresh=True)
    for f in fields:
        name = f["name"]
        if name == title_field or name in _SYSTEM_FIELD_NAMES:
            continue
        notion_prop = _pb_field_to_notion_property_definition(
            f, pb=pb, all_targets=targets,
        )
        if notion_prop is None:
            continue
        properties[snake_to_title(name)] = notion_prop

    properties.setdefault("pb_id", {"rich_text": {}})
    properties.setdefault("last_synced_at", {"date": {}})

    db = nc.create_database(
        parent_page_id=parent_page_id,
        title=db_title or snake_to_title(collection),
        properties=properties,
    )
    try:
        add_collection_to_sync_activity(nc, collection=collection)
    except Exception as e:
        print(f"[provisioner] add_collection_to_sync_activity failed: {e}")
    return db["id"]


def add_collection_to_sync_activity(
    nc: NotionClient, *, collection: str
) -> None:
    """PATCH Sync Activity DB to include `collection` as a select option."""
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    db = nc.retrieve_database(db_id)
    options = db["properties"]["collection"]["select"]["options"]
    if any(o.get("name") == collection for o in options):
        return
    new_options = options + [{"name": collection}]
    nc.update_database(db_id, {
        "properties": {"collection": {"select": {"options": new_options}}}
    })


def _get_collection(pb: PBClient, name: str) -> dict:
    return pb._http("GET", f"/api/collections/{name}")  # noqa: SLF001


def _pb_field_to_notion_property_definition(
    field: dict, *, pb: PBClient, all_targets: list,
) -> dict | None:
    """Return the Notion property body for one PB field, or None to skip."""
    ftype = field.get("type")
    name = field.get("name", "")

    if ftype in ("text", "editor", "autodate", "json"):
        return {"rich_text": {}}
    if ftype == "password":
        return None
    if ftype == "number":
        return {"number": {"format": "number"}}
    if ftype == "bool":
        return {"checkbox": {}}
    if ftype == "email":
        return {"email": {}}
    if ftype == "url":
        return {"url": {}}
    if ftype == "date":
        return {"date": {}}
    if ftype == "file":
        return {"files": {}}
    if ftype == "select":
        values = field.get("values", []) or []
        options = [{"name": v} for v in values]
        if int(field.get("maxSelect", 1) or 1) == 1:
            return {"select": {"options": options}}
        return {"multi_select": {"options": options}}
    if ftype == "relation":
        target_id = field.get("collectionId", "")
        if not target_id:
            return None
        try:
            target_coll = pb._http("GET", f"/api/collections/{target_id}")  # noqa: SLF001
            target_name = target_coll.get("name", "")
        except Exception:
            return None
        target = next((t for t in all_targets
                       if t.collection == target_name and t.enabled), None)
        if not target or not target.notion_db_id:
            print(f"[provisioner] skipping relation field {name!r} — "
                   f"target {target_name!r} is not synced")
            return None
        return {"relation": {
            "database_id": target.notion_db_id,
            "single_property": {},
        }}
    # Unknown type — safe fallback.
    print(f"[provisioner] unknown PB type {ftype!r} for field {name!r}; "
          f"falling back to rich_text")
    return {"rich_text": {}}
```

- [ ] **Step 6.4: Run tests to verify they pass**

Run: `python -m pytest tests/notion_sync/test_provisioner.py -v`
Expected: 9 passed.

- [ ] **Step 6.5: Commit**

```bash
git add notion_sync/provisioner.py tests/notion_sync/test_provisioner.py
git commit -m "notion_sync/provisioner.py: auto-create Notion DB matching a PB collection"
```

---

## Task 7: REST endpoints in server.py

**Files:**
- Modify: `server.py`

**References:** Spec §7.

- [ ] **Step 7.1: Locate the insertion point + check imports**

In `server.py`, locate the existing `@app.patch("/api/sync/settings")` handler (around line 1547+). New endpoints go **immediately after** the last existing `/api/sync/*` handler.

Check existing imports:

```bash
grep -nE "from fastapi|JSONResponse|HTTPException|from notion_sync" server.py | head -20
```

If `JSONResponse` or `HTTPException` is missing, add at the top of `server.py`:

```python
from fastapi import HTTPException
from fastapi.responses import JSONResponse
```

Always add these three (idempotent if already present):

```python
import notion_sync.config as sync_config_registry
from notion_sync.notion_api import NotionClient
from notion_sync.provisioner import provision_notion_db
```

(If `PBClient` isn't already imported from `notion_sync.pb_api`, add that too — check via grep first.)

- [ ] **Step 7.2: Add the helper + system collection blocklist**

Add right above the new endpoint definitions:

```python
_SYSTEM_PB_COLLECTIONS = {
    "sync_config", "sync_global",
    "_pb_users_auth_", "_superusers", "_mfas", "_otps",
    "_authOrigins", "_externalAuths",
}


def _pb_collection_field_names(pb: PBClient, name: str) -> set[str]:
    """Field names of one PB collection. Raises if not found."""
    raw = pb._http("GET", f"/api/collections/{name}")  # noqa: SLF001
    return {f["name"] for f in raw.get("fields", [])}
```

- [ ] **Step 7.3: Add `GET /api/sync/targets`**

```python
@app.get("/api/sync/targets")
async def api_sync_targets_list():
    """List configured sync targets + PB collections still available to enable."""
    def _do():
        pb = PBClient()
        targets = sync_config_registry.load_all(pb, fresh=True)
        configured = [
            {
                "id": t.id, "collection": t.collection,
                "notion_db_id": t.notion_db_id,
                "enabled": t.enabled, "auto_sync": t.auto_sync,
                "title_field": t.title_field, "date_field": t.date_field,
                "field_map_overrides": t.field_map_overrides,
                "last_synced_at": t.last_synced_at,
                "last_sync_summary": t.last_sync_summary,
            }
            for t in targets
        ]
        configured_names = {t.collection for t in targets}
        all_colls = pb.list_collections()
        available = []
        for c in all_colls:
            if c.get("type") != "base":
                continue
            name = c.get("name", "")
            if not name or name in _SYSTEM_PB_COLLECTIONS or name in configured_names:
                continue
            fields = []
            for f in c.get("fields", []):
                spec = {"name": f["name"], "type": f["type"]}
                if f.get("required"): spec["required"] = True
                if f["type"] == "select":
                    spec["values"] = f.get("values", [])
                    spec["maxSelect"] = f.get("maxSelect", 1)
                fields.append(spec)
            available.append({"collection": name, "fields": fields})
        return {"configured": configured, "available": available}
    return await asyncio.to_thread(_do)
```

- [ ] **Step 7.4: Add `POST /api/sync/targets`**

```python
@app.post("/api/sync/targets")
async def api_sync_targets_create(body: dict | None = None):
    """End-to-end: provision Notion DB + insert sync_config + spawn reconcile."""
    body = body or {}
    collection  = (body.get("collection")  or "").strip()
    title_field = (body.get("title_field") or "").strip()
    date_field  = (body.get("date_field")  or "").strip()
    auto_sync   = bool(body.get("auto_sync"))
    if not collection or not title_field:
        return JSONResponse({"error": "collection and title_field required"},
                             status_code=400)

    def _validate_and_provision():
        pb = PBClient()
        nc = NotionClient()
        fields = _pb_collection_field_names(pb, collection)
        if title_field not in fields:
            raise HTTPException(status_code=400,
                detail=f"title_field={title_field!r} not on {collection!r}")
        if date_field and date_field not in fields:
            raise HTTPException(status_code=400,
                detail=f"date_field={date_field!r} not on {collection!r}")
        existing = sync_config_registry.get(collection, pb, fresh=True)
        if existing is not None:
            raise HTTPException(status_code=409,
                detail=f"sync_config row for {collection!r} already exists")
        notion_db_id = provision_notion_db(
            pb=pb, nc=nc, collection=collection, title_field=title_field,
        )
        pb.create_record("sync_config", {
            "collection": collection, "notion_db_id": notion_db_id,
            "enabled": True, "auto_sync": auto_sync,
            "title_field": title_field, "date_field": date_field,
            "field_map_overrides": {},
        })
        sync_config_registry.invalidate()
        return notion_db_id

    try:
        notion_db_id = await asyncio.to_thread(_validate_and_provision)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    asyncio.create_task(_spawn_reconcile_initial(collection))
    return {"ok": True, "notion_db_id": notion_db_id, "reconcile_started": True}


async def _spawn_reconcile_initial(collection: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/home/dev/phone-bridge/.venv/bin/python",
            "scripts/reconcile_initial.py", "--only", collection,
            cwd="/home/dev/phone-bridge",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=600)
    except Exception as e:
        log.warning("reconcile_initial spawn for %s failed: %s", collection, e)
```

- [ ] **Step 7.5: Add `PATCH /api/sync/targets/{collection}`**

```python
@app.patch("/api/sync/targets/{collection}")
async def api_sync_targets_patch(collection: str, body: dict | None = None):
    body = body or {}
    allowed = {"enabled", "auto_sync", "title_field", "date_field",
                "field_map_overrides"}
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        return JSONResponse({"error": "no recognized keys"}, status_code=400)

    def _do():
        pb = PBClient()
        rows = pb.list_records("sync_config",
                                filter=f"collection='{collection}'", sort="")
        if not rows:
            raise HTTPException(status_code=404,
                detail=f"no sync_config for {collection!r}")
        row_id = rows[0]["id"]
        if "title_field" in patch or "date_field" in patch:
            fields = _pb_collection_field_names(pb, collection)
            tf = patch.get("title_field", rows[0].get("title_field"))
            df = patch.get("date_field",  rows[0].get("date_field"))
            if tf and tf not in fields:
                raise HTTPException(status_code=400,
                    detail=f"title_field={tf!r} not on {collection!r}")
            if df and df not in fields:
                raise HTTPException(status_code=400,
                    detail=f"date_field={df!r} not on {collection!r}")
        updated = pb.update_record("sync_config", row_id, patch)
        sync_config_registry.invalidate()
        return updated

    try:
        return await asyncio.to_thread(_do)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 7.6: Add `DELETE /api/sync/targets/{collection}`**

```python
@app.delete("/api/sync/targets/{collection}")
async def api_sync_targets_delete(collection: str):
    def _do():
        pb = PBClient()
        rows = pb.list_records("sync_config",
                                filter=f"collection='{collection}'", sort="")
        if not rows:
            raise HTTPException(status_code=404,
                detail=f"no sync_config for {collection!r}")
        notion_db_id = rows[0].get("notion_db_id", "")
        pb.delete_record("sync_config", rows[0]["id"])
        sync_config_registry.invalidate()
        return {"ok": True, "notion_db_id": notion_db_id}
    try:
        return await asyncio.to_thread(_do)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 7.7: Add `POST /api/sync/registry/export-snapshot`**

```python
@app.post("/api/sync/registry/export-snapshot")
async def api_sync_registry_export_snapshot():
    """Run scripts/dump_sync_registry.py and return the output path."""
    out_path = "notion_sync/registry.snapshot.yaml"
    cmd = ["/home/dev/phone-bridge/.venv/bin/python",
            "scripts/dump_sync_registry.py", "--path", out_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd="/home/dev/phone-bridge",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return JSONResponse(
                {"ok": False, "error": stderr.decode("utf-8", "replace")[:500]},
                status_code=500,
            )
        return {"ok": True, "path": out_path}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
```

- [ ] **Step 7.8: Deploy + smoke-test each endpoint**

Run `deploy`, then:

```bash
ssh dashboard-server 'curl -sS http://127.0.0.1:8001/api/sync/targets | python3 -m json.tool | head -40'
```
Expected: JSON with `configured: [...]` (8 entries) + `available: [...]`.

```bash
ssh dashboard-server 'curl -sS -X PATCH -H "Content-Type: application/json" \
  http://127.0.0.1:8001/api/sync/targets/contacts -d "{\"auto_sync\":true}" | python3 -m json.tool'
ssh dashboard-server 'curl -sS -X PATCH -H "Content-Type: application/json" \
  http://127.0.0.1:8001/api/sync/targets/contacts -d "{\"auto_sync\":false}" | python3 -m json.tool'
```
Expected: each returns the updated row. (Second call reverts the change so we don't leave state behind.)

```bash
ssh dashboard-server 'curl -sS -X POST http://127.0.0.1:8001/api/sync/registry/export-snapshot'
```
Expected at this stage: 500 — the script doesn't exist yet (Task 9 creates it). That's OK; the endpoint exists and routes correctly.

- [ ] **Step 7.9: Commit**

```bash
git add server.py
git commit -m "server.py: REST endpoints for sync_config registry (GET/POST/PATCH/DELETE + snapshot)"
```

---

## Task 8: Settings UI extension

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`
- Modify: `static/style.css`

**References:** Spec §8.

- [ ] **Step 8.1: Bump cache-buster versions**

In `static/index.html`, change `style.css?v=42` → `?v=43`, `icons.js?v=42` → `?v=43`, `app.js?v=43` → `?v=44`. (Look in the bottom `<script>` lines and the `<link rel="stylesheet">` line.)

- [ ] **Step 8.2: Add the new dialog markup**

In `static/index.html`, after the existing `<dialog id="checkin-dialog">…</dialog>` block (closes around line 198), add:

```html
    <!-- Add-sync-target dialog: opened from inside the sync-settings modal. -->
    <dialog id="sync-add-dialog" class="sync-add-dialog">
      <form method="dialog" class="sa-form">
        <header class="sa-head">
          <span class="sa-title">新增同步</span>
          <button class="icon-btn" value="cancel" type="submit"
                  aria-label="取消" data-icon="close"></button>
        </header>
        <div class="sa-fields">
          <label class="sa-row">
            <span>PB 集合</span>
            <select id="sa-collection"></select>
          </label>
          <label class="sa-row">
            <span>Notion 标题列 (title_field)</span>
            <select id="sa-title-field"></select>
          </label>
          <label class="sa-row">
            <span>日期列 (date_field, 可选)</span>
            <select id="sa-date-field"></select>
          </label>
          <label class="sa-row sa-toggle">
            <input id="sa-auto-sync" type="checkbox" checked>
            <span>自动同步 (写 PB 立即推 Notion)</span>
          </label>
        </div>
        <div class="sa-submit-bar">
          <button id="sa-submit" type="button" class="sa-go">创建并同步</button>
        </div>
      </form>
    </dialog>
```

- [ ] **Step 8.3: Add the sync-targets section to the sync-settings modal**

The existing sync-settings modal is rendered dynamically in `app.js` (search for `'sync-settings'` in the menu handler around line 1504). Locate the function that builds that modal's HTML. After its existing controls (timezone / hours / paused), insert this HTML fragment:

```html
<div id="sync-targets-section" class="sync-targets-section">
  <h4>同步表</h4>
  <div id="sync-targets-tbody" class="sync-targets-tbody">
    <div class="sync-targets-loading">加载中…</div>
  </div>
  <button id="sync-targets-add" type="button" class="sync-targets-add">
    + 新增同步表
  </button>
</div>
```

- [ ] **Step 8.4: Add the JS helpers to app.js**

In `static/app.js`, add at module scope (near the existing sync-settings helpers — typically toward the bottom of the file but above the bottom event-binding section):

```javascript
async function loadSyncTargets() {
  const tbody = document.getElementById('sync-targets-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<div class="sync-targets-loading">加载中…</div>';
  try {
    const r = await fetch(apiUrl('/api/sync/targets'));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    renderSyncTargets(data);
    window.__sync_available = data.available || [];
  } catch (e) {
    tbody.innerHTML = '<div class="sync-targets-error">同步配置读取失败: '
                     + escapeHtml(String(e)) + '</div>';
  }
}

function renderSyncTargets(data) {
  const tbody = document.getElementById('sync-targets-tbody');
  if (!tbody) return;
  const rows = (data.configured || []).map(t => `
    <div class="st-row" data-collection="${escapeHtml(t.collection)}">
      <span class="st-name">${escapeHtml(t.collection)}</span>
      <label class="st-check">
        <input type="checkbox" data-key="enabled" ${t.enabled ? 'checked' : ''}>启用
      </label>
      <label class="st-check">
        <input type="checkbox" data-key="auto_sync" ${t.auto_sync ? 'checked' : ''}>自动
      </label>
      <input class="st-field" data-key="title_field"
             value="${escapeHtml(t.title_field || '')}" placeholder="title_field">
      <input class="st-field" data-key="date_field"
             value="${escapeHtml(t.date_field || '')}" placeholder="date_field">
      <button class="st-del" type="button" aria-label="删除">✕</button>
    </div>
  `).join('');
  tbody.innerHTML = rows || '<div class="sync-targets-empty">还没有同步表</div>';
  tbody.querySelectorAll('.st-row').forEach(rowEl => {
    const col = rowEl.dataset.collection;
    rowEl.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener('change', () =>
        patchSyncTarget(col, { [cb.dataset.key]: cb.checked }));
    });
    rowEl.querySelectorAll('input.st-field').forEach(inp => {
      inp.addEventListener('change', () =>
        patchSyncTarget(col, { [inp.dataset.key]: inp.value.trim() }));
    });
    rowEl.querySelector('.st-del').addEventListener('click', () =>
      confirmDeleteSyncTarget(col));
  });
}

async function patchSyncTarget(collection, patch) {
  try {
    const r = await fetch(apiUrl('/api/sync/targets/' + encodeURIComponent(collection)), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status + ' — ' + await r.text());
  } catch (e) {
    alert('保存失败: ' + e);
    loadSyncTargets();        // re-render with server's actual state
  }
}

async function confirmDeleteSyncTarget(collection) {
  if (!confirm('停止同步 `' + collection + '`?\nNotion DB 将保留(不会删除)。')) return;
  try {
    const r = await fetch(apiUrl('/api/sync/targets/' + encodeURIComponent(collection)),
                          { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status + ' — ' + await r.text());
    loadSyncTargets();
  } catch (e) {
    alert('删除失败: ' + e);
  }
}

function openAddSyncTarget() {
  const dlg = document.getElementById('sync-add-dialog');
  const selColl = document.getElementById('sa-collection');
  const selTitle = document.getElementById('sa-title-field');
  const selDate  = document.getElementById('sa-date-field');
  const cbAuto   = document.getElementById('sa-auto-sync');
  selColl.innerHTML = '';
  (window.__sync_available || []).forEach(av => {
    const opt = document.createElement('option');
    opt.value = av.collection;
    opt.textContent = av.collection;
    selColl.appendChild(opt);
  });
  function refreshFieldDropdowns() {
    const sel = (window.__sync_available || []).find(a => a.collection === selColl.value);
    selTitle.innerHTML = '';
    selDate.innerHTML  = '<option value="">— (不用日期)</option>';
    (sel ? sel.fields : []).forEach(f => {
      const ot = document.createElement('option');
      ot.value = f.name; ot.textContent = f.name + ' (' + f.type + ')';
      if (f.type === 'text' && (f.name === 'title' || f.name === 'name')) {
        ot.selected = true;
      }
      selTitle.appendChild(ot);
      if (f.type === 'date') {
        const od = document.createElement('option');
        od.value = f.name; od.textContent = f.name; selDate.appendChild(od);
      }
    });
  }
  selColl.onchange = refreshFieldDropdowns;
  refreshFieldDropdowns();
  cbAuto.checked = true;
  dlg.showModal();
  document.getElementById('sa-submit').onclick = async () => {
    const payload = {
      collection: selColl.value,
      title_field: selTitle.value,
      date_field: selDate.value,
      auto_sync: cbAuto.checked,
    };
    try {
      const r = await fetch(apiUrl('/api/sync/targets'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || body.error || ('HTTP ' + r.status));
      dlg.close();
      alert('已创建,后台正在做首次对齐');
      loadSyncTargets();
    } catch (e) {
      alert('创建失败: ' + e);
    }
  };
}
```

- [ ] **Step 8.5: Wire the loader to sync-settings open**

In the `'sync-settings'` cmd handler in `app.js` (around line 1504), AFTER the existing modal-rendering code, append:

```javascript
        loadSyncTargets();
        const addBtn = document.getElementById('sync-targets-add');
        if (addBtn) addBtn.addEventListener('click', openAddSyncTarget);
```

(Indentation should match the surrounding `if (cmd === 'sync-settings') { ... }` block.)

- [ ] **Step 8.6: Add CSS**

Append to `static/style.css`:

```css
.sync-targets-section { margin-top: 24px; }
.sync-targets-section h4 { margin: 0 0 8px; font-size: 14px; color: #888; }
.sync-targets-tbody { display: flex; flex-direction: column; gap: 6px; }
.sync-targets-loading, .sync-targets-empty, .sync-targets-error {
  font-size: 13px; color: #666; padding: 8px;
}
.sync-targets-error { color: #c33; }
.st-row {
  display: grid;
  grid-template-columns: 80px auto auto 1fr 1fr 28px;
  gap: 8px; align-items: center;
  font-size: 13px;
  padding: 6px 8px; border-radius: 6px; background: #1a1a1a;
}
.st-name { font-weight: 500; }
.st-check { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.st-field {
  background: #0f0f0f; border: 1px solid #333; color: #eee;
  padding: 4px 6px; border-radius: 4px; min-width: 0;
}
.st-del {
  background: transparent; border: 1px solid #444; color: #c33;
  width: 24px; height: 24px; border-radius: 4px; cursor: pointer;
}
.st-del:hover { background: #2a1414; }
.sync-targets-add {
  margin-top: 8px; background: #1a3a1a; border: 1px solid #2a5a2a;
  color: #6c6; padding: 6px 12px; border-radius: 6px; cursor: pointer;
}
.sync-add-dialog::backdrop { background: rgba(0,0,0,0.5); }
.sa-form { display: flex; flex-direction: column; gap: 12px; padding: 16px;
           min-width: 320px; background: #0f0f0f; color: #eee; }
.sa-head { display: flex; align-items: center; justify-content: space-between; }
.sa-fields { display: flex; flex-direction: column; gap: 10px; }
.sa-row { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.sa-row select, .sa-row input[type=text] {
  background: #1a1a1a; border: 1px solid #333; color: #eee;
  padding: 6px; border-radius: 4px;
}
.sa-toggle { flex-direction: row; align-items: center; gap: 8px; }
.sa-submit-bar { display: flex; justify-content: flex-end; }
.sa-go {
  background: #2a5a2a; border: none; color: white;
  padding: 8px 16px; border-radius: 6px; cursor: pointer;
}
```

- [ ] **Step 8.7: Deploy + manual end-to-end test**

Run `deploy`. Then on phone or laptop:

1. Open Phone Bridge → 菜单 → 同步设置. Verify the "同步表" section appears and lists 8 rows with their seeded values.
2. Toggle `auto_sync` off on `trips`. Close + reopen 同步设置. Verify it stayed off.
3. Toggle it back on.
4. In Phone Bridge chat:

   > "Make a PB collection called `pb_sync_test_table_x` with two fields: `title` (text, required) and `note` (text)."

   Claude calls `pb_create_collection`.
5. Open 同步设置 → click **+ 新增同步表** → select `pb_sync_test_table_x` → confirm `title_field=title`, leave `date_field=—`, `auto_sync=on` → click **创建并同步**.
6. Verify in Notion: a new DB `Pb Sync Test Table X` (or similar) appears under the parent page with `Title`, `Note`, `pb_id`, `last_synced_at` columns. Sync Activity DB's `collection` select includes `pb_sync_test_table_x`.
7. Click the ✕ on that row in 同步设置 → confirm deletion. Notion DB should **still exist** (was kept by design).

- [ ] **Step 8.8: Clean up the test collection**

In Phone Bridge chat:

> "Delete the PB collection `pb_sync_test_table_x` (it was a test)."

Claude calls `pb_delete_collection`. Manually archive the orphaned Notion DB if you want to be tidy.

- [ ] **Step 8.9: Commit**

```bash
git add static/index.html static/app.js static/style.css
git commit -m "static/: 同步设置 page — sync targets table + 新增同步 dialog"
```

---

## Task 9: scripts/dump_sync_registry.py + first snapshot

**Files:**
- Create: `scripts/dump_sync_registry.py`
- Create: `notion_sync/registry.snapshot.yaml`

**References:** Spec §9.

- [ ] **Step 9.1: Create the script**

Path: `scripts/dump_sync_registry.py`:

```python
#!/usr/bin/env python3
"""Dump PB sync_config + sync_global to a YAML snapshot.

Output: notion_sync/registry.snapshot.yaml (committed to git for disaster
recovery). Manual — runs only when invoked, not on PB writes.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.pb_api import PBClient


HEADER = """\
# Auto-generated by scripts/dump_sync_registry.py.
# Source of truth: PB sync_config / sync_global tables.
# Regenerate after every UI change:
#   python scripts/dump_sync_registry.py
# and commit the diff so disaster-recovery git can rebuild PB.
"""


def _yaml_scalar(v) -> str:
    """Encode a single value (str, int, bool, None) as a YAML scalar."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if (s == "" or any(c in s for c in ":#\n") or s[:1] in (" ", "-", "?", "@")
        or s.lower() in ("null", "true", "false", "yes", "no")):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def _emit_dict_inline(d: dict) -> str:
    if not d:
        return "{}"
    parts = [f'{k}: {_yaml_scalar(v)}' for k, v in d.items()]
    return "{" + ", ".join(parts) + "}"


def render(sync_global: dict | None, targets: list[dict]) -> str:
    lines = [HEADER.rstrip(), ""]
    lines.append(f'generated_at: "{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    lines.append("sync_global:")
    g = sync_global or {}
    for key in ("timezone", "sync_hour_local", "sync_hour_local_2",
                "paused", "last_run_at"):
        lines.append(f"  {key}: {_yaml_scalar(g.get(key))}")
    lines.append("sync_targets:")
    for t in targets:
        lines.append(f"  - collection: {_yaml_scalar(t.get('collection'))}")
        for key in ("notion_db_id", "enabled", "auto_sync",
                    "title_field", "date_field"):
            lines.append(f"    {key}: {_yaml_scalar(t.get(key))}")
        overrides = t.get("field_map_overrides") or {}
        lines.append(f"    field_map_overrides: {_emit_dict_inline(overrides)}")
        for key in ("last_synced_at", "last_sync_summary"):
            lines.append(f"    {key}: {_yaml_scalar(t.get(key))}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="notion_sync/registry.snapshot.yaml")
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    try:
        pb = PBClient()
        globals_ = pb.list_records("sync_global", sort="")
        sync_global = globals_[0] if globals_ else None
        targets = pb.list_records("sync_config", sort="collection")
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    text = render(sync_global, targets)
    if args.stdout:
        sys.stdout.write(text)
    else:
        out = Path(args.path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 9.2: Generate the first snapshot (run against the VM)**

The script needs PB credentials, which only live on the VM. Run it remotely and capture the output locally:

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && set -a && . ./.env && set +a && \
  .venv/bin/python scripts/dump_sync_registry.py --stdout' > notion_sync/registry.snapshot.yaml
```

Verify:

```bash
head -30 notion_sync/registry.snapshot.yaml
```

Expected: the header comment, then `generated_at`, `sync_global:`, then 8 entries under `sync_targets:` sorted by collection name (contacts, days, journal, locations, plans, stops, todos, trips).

- [ ] **Step 9.3: Verify the REST endpoint now works**

```bash
ssh dashboard-server 'curl -sS -X POST http://127.0.0.1:8001/api/sync/registry/export-snapshot'
```

Expected: `{"ok": true, "path": "notion_sync/registry.snapshot.yaml"}` (after deploy in step 9.5 the script will exist on the VM).

- [ ] **Step 9.4: Commit script + snapshot**

```bash
git add scripts/dump_sync_registry.py notion_sync/registry.snapshot.yaml
git commit -m "scripts/dump_sync_registry.py + first snapshot of registry state"
```

- [ ] **Step 9.5: Deploy + confirm**

Run `deploy`. The snapshot YAML is part of the repo so it goes with deploy.

```bash
ssh dashboard-server 'ls -la /home/dev/phone-bridge/notion_sync/registry.snapshot.yaml'
```

Expected: file present, recent timestamp.

---

## Task 10: Documentation updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/notion-pb-sync.md`
- Modify: `docs/data-model.md`
- Modify: `scripts/setup_notion_sync_db.py` (comment expansion only)

**References:** Spec §10.

- [ ] **Step 10.1: Update CLAUDE.md**

In `CLAUDE.md`, find the section "Notion sync" (the one mentioning PR1+PR2+PR3). Add a new subsection at the **end** of that section, before the next major heading:

```markdown
### Sync registry (where the list of synced tables lives)

As of 2026-06-04 the per-target sync configuration lives entirely in the
PB `sync_config` table — three new columns (`title_field`, `date_field`,
`auto_sync`) replace what used to be hardcoded Python dicts in three
files. To add a new sync target:

1. Create the PB collection (chat with Claude → `pb_create_collection`)
2. Open Phone Bridge → 同步设置 → click **+ 新增同步表** → pick the new
   collection, set title_field / auto_sync, hit "创建并同步"
3. The server auto-creates the matching Notion DB (with pb_id +
   last_synced_at pipeline columns), inserts the sync_config row, and
   spawns `reconcile_initial --only <new>`

No code changes required. See
[`docs/sync-registry-design.md`](docs/sync-registry-design.md) for the
field-by-field design, the PB→Notion type mapping table, relation
handling rules, and the REST API reference.

**Disaster-recovery snapshot:** the runtime state of the registry is
mirrored to `notion_sync/registry.snapshot.yaml`. It's NOT auto-updated
— after any UI change you care about, run
`python scripts/dump_sync_registry.py` and commit the diff.
```

- [ ] **Step 10.2: Update docs/notion-pb-sync.md**

Find the section that describes adding a sync target (or, if none, find where the 8 synced collections are listed). Add or replace with:

```markdown
## Adding a new sync target

Since 2026-06-04, registering a new sync target is a 2-step user flow,
not a code change:

1. **Create the PB collection** via the Phone Bridge chat:

   > "Make a PB collection called `<name>` with fields …"

   Claude calls `pb_create_collection`. PB writes a JS migration file
   that the next deploy will pull back into git.

2. **Register it for sync** via Phone Bridge → 同步设置 → "+ 新增同步表".
   Pick the new collection, set `title_field`, optionally `date_field`,
   leave `auto_sync` on (default). Click "创建并同步".

The server auto-provisions a Notion DB matching the PB schema, inserts
a `sync_config` row, adds the collection name to Sync Activity's select,
and runs `reconcile_initial --only <new>` in the background.

See [`docs/sync-registry-design.md`](sync-registry-design.md) for the
mechanism — REST endpoints, PB→Notion type mapping table, relation
handling, and out-of-scope notes.
```

- [ ] **Step 10.3: Update docs/data-model.md**

Find the section describing the `sync_config` table fields (likely a table). Add three rows:

```markdown
| `title_field` | text | The PB field used as the Notion title column. Required. Seeded by migration `1779465623` (e.g. `trips → "title"`, `days → "name"`). |
| `date_field`  | text | The PB field used for ordering / fuzzy matching during reconcile. Empty for `contacts` and `locations`. |
| `auto_sync`   | bool | When true, a PB write via `mcp__pb__pb_create / pb_update / pb_delete` schedules a 10-second-debounced runner pass for this collection. When false, the row waits for the next cron tick. |
```

- [ ] **Step 10.4: Expand the setup_notion_sync_db.py comment**

In `scripts/setup_notion_sync_db.py`, find the comment block above `SYNC_TARGETS: dict[str, str] = {` (around line 27). After the existing paragraph, append:

```python
# After 2026-06-04 the per-target metadata (title_field, date_field,
# auto_sync) lives in extra columns on sync_config. This bootstrap
# script does NOT seed those — the migration
# `1779465623_extend_sync_config.js` does. See
# docs/sync-registry-design.md.
```

- [ ] **Step 10.5: Commit**

```bash
git add CLAUDE.md docs/notion-pb-sync.md docs/data-model.md scripts/setup_notion_sync_db.py
git commit -m "docs: sync registry — CLAUDE.md / notion-pb-sync.md / data-model.md / setup script comment"
```

- [ ] **Step 10.6: Final deploy + full smoke test**

Run `deploy`. Then verify the whole stack:

```bash
ssh dashboard-server 'curl -sS http://127.0.0.1:8001/api/sync/targets | python3 -m json.tool | head -20'
ssh dashboard-server 'curl -sS -X POST http://127.0.0.1:8001/api/sync/now | python3 -m json.tool'
ssh dashboard-server 'tail -3 /home/dev/phone-bridge/.bridge_data/sync.log'
```

Expected: targets list returns 8 entries, sync_now exits OK with `applied/conflicts/deletes` summary, log shows `run_end` with sensible counts.

---

## Acceptance criteria

The implementation is complete when **all** of the following are true. Use this as the implementing agent's hallucination guard (cross-references spec §14):

- [ ] Migration timestamp is exactly `1779465623` and is checked into git.
- [ ] `notion_sync/runner.py` does NOT contain the string `TITLE_FIELD_BY_COLLECTION`.
- [ ] `scripts/reconcile_initial.py` does NOT contain `DATE_FIELD_BY_COLLECTION`.
- [ ] `pb_tools.py` does NOT contain the literal set `{"trips", "days", "stops", "locations", "todos", "journal"}`.
- [ ] `notion_sync/config.py` exports exactly: `SyncTarget`, `load_all`, `load_enabled`, `get`, `collections_with_auto_sync`, `invalidate`.
- [ ] `notion_sync/provisioner.py` exports `provision_notion_db`, `add_collection_to_sync_activity`.
- [ ] All five new REST endpoints are reachable: `GET /api/sync/targets`, `POST /api/sync/targets`, `PATCH /api/sync/targets/{c}`, `DELETE /api/sync/targets/{c}`, `POST /api/sync/registry/export-snapshot`.
- [ ] `DELETE` does NOT archive the Notion DB.
- [ ] `requirements.txt` is unchanged (no PyYAML added).
- [ ] `notion_sync/registry.snapshot.yaml` exists in the repo and reflects the live PB state at the time of the last `dump_sync_registry.py` run.
- [ ] All tests pass: `python -m pytest tests/notion_sync/ -v`
- [ ] Manual UI test (Task 8.7) succeeded: created + deleted a test sync target end-to-end from the phone.

If any of these is `unsure`, stop and ask the user.
