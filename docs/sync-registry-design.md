# Sync Registry — Design Spec

**Date:** 2026-06-04
**Status:** Approved for implementation (not yet built)
**Author:** brainstorming session, Phone Bridge project

> ⚠️ **For implementation agents:** this document is **the** specification.
> Every file path, function signature, JSON shape, migration body, and REST
> endpoint is intentionally explicit. Do **not** invent collection names,
> field names, return shapes, or behavior that is not written here. If
> something is ambiguous, stop and ask the user — do not guess.

---

## 1. Background

Today the list of "what gets synced to Notion" is encoded in **three**
hardcoded Python data structures, plus an undocumented bootstrap dict:

| Location | Symbol | Lines (current main) |
|---|---|---|
| `notion_sync/runner.py` | `TITLE_FIELD_BY_COLLECTION: dict[str, str]` | lines 55–58 |
| `scripts/reconcile_initial.py` | `TITLE_FIELD_BY_COLLECTION` + `DATE_FIELD_BY_COLLECTION` | lines 42–51 |
| `pb_tools.py` | `_AUTO_SYNC_COLLECTIONS: set[str]` | lines 51–53 |
| `scripts/setup_notion_sync_db.py` | `SYNC_TARGETS: dict[str, str]` | lines 35–45 |

Adding a 9th sync target today therefore requires editing 4 files + 1 PB
migration + 1 Notion DB creation done by hand. The user's quote captures
the goal:

> 现在的代码， 如果以后我要添加新的数据库， 也要同步，会需要改很多吗？如果都是写在代码里话，后面需要模块化

And the desired developer-facing flow:

> 需要一个库来记载每个表的结构，和需要同步的部分， 如果加新表后， 只需要在这个库里登记新的表格名称和结构

After clarifying questions the user landed on these decisions:

| Decision | User's choice | Direct quote |
|---|---|---|
| Granularity | Open (sync all PB columns by default, skip relations to non-synced targets) | "不用，保持开放式就好。" |
| Source of truth | PB `sync_config` table (extended with new columns) + manual YAML snapshot to git | "A+ — PB 为主 + 可导出 snapshot 到 git" |
| Scope | Include the settings UI in this work | "一次性做全 — 连设置页面一起上" |
| Relation handling | Target also synced → Notion `relation`; target not synced → skip column | (answered Q4) |
| Unsync action | Keep Notion DB (do not archive) | (answered Q5) |
| Snapshot trigger | Manual via `scripts/dump_sync_registry.py` | (answered Q6) |
| Spec location | `docs/sync-registry-design.md` (top-level) | (answered Q7) |

And the corrected workflow once this lands (per the user's latest
clarifications, quoted in §15):

```
You (in Phone Bridge chat)              Claude / server
────────────────────────────────         ────────────────────────────────
1. "我要一个表存 XYZ ..."        →     chat agent calls pb_create_collection
                                          (PB writes the collection live;
                                           PB auto-writes a pb_migrations/*.js
                                           that the next deploy pulls back
                                           into git)
2. Open "同步设置" page          →     UI lists every PB base collection,
                                          flags which ones lack a sync_config row
3. Click "启用同步" on the new   →     UI shows a form:
   collection, fill                       - title_field (auto-suggest)
   title_field / date_field /             - date_field (auto-suggest or empty)
   auto_sync                              - auto_sync (default true)
4. Click "创建并同步"            →     Server, in one POST:
                                          a. POST Notion API → new DB under
                                             NOTION_SYNC_PARENT_PAGE_ID, with
                                             columns inferred from PB schema
                                             + pb_id + last_synced_at
                                          b. INSERT sync_config row with
                                             collection / notion_db_id /
                                             title_field / date_field /
                                             auto_sync / enabled=true
                                          c. PATCH Sync Activity DB → add the
                                             new collection name to the
                                             `collection` select options
                                          d. Spawn reconcile_initial --only X
                                          e. Return { ok, notion_db_id,
                                                       reconcile_started }
5. Done. Notion shows the new DB.
   Future PB writes auto-sync; future Notion edits land in
   Sync Activity for review.
```

The user **never touches Notion** in this flow. They only chat (to create
the PB collection) and click (to enable sync). PB migrations are still
git-versioned via the deploy round-trip; that is not changing.

---

## 2. Component map

| ID | Component | New / existing | Path |
|---|---|---|---|
| A  | PB migration 1779465623 — extend `sync_config` schema | **new** | `pocketbase/pb_migrations/1779465623_extend_sync_config.js` |
| B  | Registry loader module | **new** | `notion_sync/config.py` |
| C  | Notion DB auto-provisioner | **new** | `notion_sync/provisioner.py` |
| D  | Snapshot dumper script | **new** | `scripts/dump_sync_registry.py` |
| E  | Snapshot output file | **new** (generated, git-tracked) | `notion_sync/registry.snapshot.yaml` |
| F  | Refactor — drop hardcoded `TITLE_FIELD_BY_COLLECTION` in runner | edit | `notion_sync/runner.py` |
| G  | Refactor — drop hardcoded title/date maps in reconcile | edit | `scripts/reconcile_initial.py` |
| H  | Refactor — make `_AUTO_SYNC_COLLECTIONS` dynamic | edit | `pb_tools.py` |
| I  | New REST endpoints for the settings UI | edit | `server.py` |
| J  | Settings UI extension (sync targets table) | edit | `static/index.html`, `static/app.js`, `static/style.css` |
| K  | CLAUDE.md / data-model.md updates | edit | `CLAUDE.md`, `docs/data-model.md`, `docs/notion-pb-sync.md` |
| L  | Tests for registry loader + provisioner | **new** | `tests/notion_sync/test_config.py`, `tests/notion_sync/test_provisioner.py` |

`scripts/setup_notion_sync_db.py` is intentionally NOT touched in this
change — it remains the bootstrap script for a clean workspace. Its
`SYNC_TARGETS` dict stays, with the existing "NOT THE SOURCE OF TRUTH"
comment expanded to point at this spec.

---

## 3. Component A — PB migration `1779465623_extend_sync_config.js`

### 3.1 Filename & timestamp

Next available timestamp after `1779465622_add_second_sync_hour.js`. Use
exactly **`1779465623_extend_sync_config.js`**.

### 3.2 Schema delta

Add **three** fields to `sync_config`, in this order, before the existing
`created` / `updated` autodate fields:

| Field name | PB type | Required | Default | Notes |
|---|---|---|---|---|
| `title_field` | `text` (max 60) | true | (seeded per-row; see 3.4) | The PB field used as the Notion title for this collection. |
| `date_field`  | `text` (max 60) | false | "" (or null) | The PB field used for ordering / matching during reconcile. Empty string means "no date field" (e.g. contacts, locations). |
| `auto_sync`   | `bool`          | false | (seeded per-row; see 3.4) | When true, a write to this collection via `mcp__pb__pb_create / pb_update / pb_delete` triggers the debounced runner. |

Do NOT add `sync_direction`, `debounce_seconds`, per-column whitelists, or
relation policy fields. They are explicitly out of scope (see §13).

### 3.3 Migration body (verbatim template)

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

  // Seed the existing 8 rows from the hardcoded values that runner.py /
  // reconcile_initial.py / pb_tools.py used pre-migration. Any row not
  // listed here is left untouched (an admin must fill in title_field
  // before it can be synced — runner will refuse with a clear error).
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
    // auto_sync: only seed if currently null (not false — false is a valid choice).
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

The seed values are **the exact union** of:
- `TITLE_FIELD_BY_COLLECTION` from `notion_sync/runner.py` lines 55–58
- `DATE_FIELD_BY_COLLECTION` from `scripts/reconcile_initial.py` lines 46–51
- `_AUTO_SYNC_COLLECTIONS` from `pb_tools.py` lines 51–53 (membership → `auto_sync=true`; absence → `false`)

Cross-check before writing: `trips, days, stops, locations, todos, journal`
are members of `_AUTO_SYNC_COLLECTIONS` → `auto_sync=true`; `plans` and
`contacts` are not → `auto_sync=false`. The seed dict above reflects that.

### 3.4 Deploy semantics

The deploy script (`deploy` tool, see `CLAUDE.md`) already copies
`pocketbase/pb_migrations/*.js` to `/opt/pocketbase/pb_migrations/`. PB
auto-runs new migrations on next start. **No manual step required.**

---

## 4. Component B — `notion_sync/config.py`

### 4.1 Purpose

One module the rest of the codebase consults to learn what gets synced.
Removes all hardcoded sync-target knowledge from `runner.py`,
`reconcile_initial.py`, `pb_tools.py`.

### 4.2 Public API (must be implemented exactly as shown)

```python
"""Sync registry — single read path for all per-collection sync metadata.

Backed by the PB `sync_config` table (one row per synced collection).
Other modules (runner, reconcile, pb_tools) MUST go through this loader
instead of caching their own dicts.

Cache: in-process 60s TTL so per-tool-call lookups don't hammer PB. The
cache key is module-level, so it's shared across import sites within
one process. `invalidate()` clears it (called by the REST handler that
mutates sync_config).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from notion_sync.pb_api import PBClient


@dataclass(frozen=True)
class SyncTarget:
    """One row of sync_config, projected into a typed shape.

    Field semantics match the PB columns one-to-one — no implicit
    transforms. `field_map_overrides` is the raw dict, not inverted.
    """
    id: str                       # PB record id (e.g. "abc123...")
    collection: str               # PB collection name (e.g. "trips")
    notion_db_id: str             # Notion database UUID
    enabled: bool
    auto_sync: bool
    title_field: str              # PB field name used as Notion title
    date_field: str               # PB field name used for matching; "" = none
    field_map_overrides: dict[str, str]   # PB-field -> Notion-property name
    last_synced_at: str           # PB autodate-formatted string (may be "")
    last_sync_summary: str        # free-form

    @property
    def overrides_inverse(self) -> dict[str, str]:
        """Notion-property -> PB-field mapping (used by notion → pb transform)."""
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
    """Return every sync_config row, including disabled ones.

    Caller filters as needed. Pass fresh=True to bypass the 60s cache.
    """
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
    """Return the SyncTarget for a single collection name, or None."""
    for t in load_all(pb, fresh=fresh):
        if t.collection == collection:
            return t
    return None


def collections_with_auto_sync(pb: PBClient | None = None,
                                *, fresh: bool = False) -> set[str]:
    """Return the set of enabled+auto_sync collection names. Used by
    pb_tools._schedule_auto_sync to decide whether a write should
    trigger the debounced runner. Enabled-but-not-auto_sync targets
    are excluded — they still get nightly cron sync."""
    return {t.collection for t in load_enabled(pb, fresh=fresh) if t.auto_sync}


def invalidate() -> None:
    """Clear the in-process cache. Call this from the REST handlers
    that mutate sync_config so the next caller sees fresh data."""
    global _cache
    _cache = None
```

### 4.3 Behavioral rules implementation agents MUST follow

- `load_all` returns **every** row including `enabled=False` ones. Filtering is the caller's job.
- The cache is process-local. `pb_tools.py` (in the bridge process) and `notion_sync/runner.py` (in a one-shot subprocess) each have their own cache. That is fine — runner runs to completion in seconds.
- `invalidate()` is called from exactly three REST handlers — the ones that mutate `sync_config`: `POST /api/sync/targets` (§7.2), `PATCH /api/sync/targets/{c}` (§7.3), `DELETE /api/sync/targets/{c}` (§7.4). Do not sprinkle other invalidation calls. The export-snapshot endpoint (§7.5) reads only and does NOT invalidate.
- Do not import any other `notion_sync.*` module from `config.py`. It must stay a leaf so importing it cannot create circular imports.

### 4.4 Error handling

`load_all` propagates exceptions raised by `PBClient.list_records`. The
PB client already wraps HTTP errors in `RuntimeError`; callers will see
them. No silent fallback to a hardcoded list — if PB is down, sync is
down, and that should fail loudly.

---

## 5. Component C — `notion_sync/provisioner.py`

### 5.1 Purpose

Given a PB collection name + a desired title/date config, **create the
matching Notion database** under the parent page referenced by
`NOTION_SYNC_PARENT_PAGE_ID`, then return its Notion DB id. Called by
the new POST endpoint in §7.2.

### 5.2 Public API

```python
"""Auto-provision a Notion database to match a PB collection.

Used when the user enables sync for a previously-not-synced collection
from the settings UI. The created DB includes pb_id + last_synced_at
pipeline columns and the right Notion property type for every PB field
(inferred from the PB schema via `pb_get_collection`-shape data).
"""
from __future__ import annotations

import os
from typing import Any

from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.config import load_all
from notion_sync.codec import snake_to_title


def provision_notion_db(
    *,
    pb: PBClient,
    nc: NotionClient,
    collection: str,
    title_field: str,
    db_title: str | None = None,
    parent_page_id: str | None = None,
) -> str:
    """Create a Notion database mirroring the PB `collection` schema.

    Returns the new Notion DB id (UUID string).

    Args:
      pb: live PB client (used to fetch the source collection schema)
      nc: live Notion client
      collection: PB collection name to mirror
      title_field: PB field used as the Notion Title column
      db_title: human-readable Notion DB name. Defaults to collection name
                in Title Case.
      parent_page_id: where to put the DB. Defaults to
                      env var NOTION_SYNC_PARENT_PAGE_ID.

    Raises:
      RuntimeError if the PB collection does not exist, if title_field
      is not a field on that collection, or if Notion API call fails.
    """
    parent_page_id = parent_page_id or os.environ["NOTION_SYNC_PARENT_PAGE_ID"]
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

    # Build the properties dict for create_database.
    properties: dict[str, dict] = {}
    properties[snake_to_title(title_field)] = {"title": {}}

    targets = load_all(pb, fresh=True)
    for f in fields:
        name = f["name"]
        if name == title_field:
            continue                                # already added as title
        if name in {"id", "created", "updated",
                    "notion_id", "notion_last_edited",
                    "last_synced_at", "pb_id"}:
            continue                                # pipeline / system fields
        notion_prop = _pb_field_to_notion_property_definition(
            f, pb=pb, all_targets=targets,
        )
        if notion_prop is None:
            continue                                # skipped (e.g. unsynced relation)
        properties[snake_to_title(name)] = notion_prop

    # Pipeline columns — required for the sync runner.
    properties.setdefault("pb_id", {"rich_text": {}})
    properties.setdefault("last_synced_at", {"date": {}})

    db = nc.create_database(
        parent_page_id=parent_page_id,
        title=db_title or snake_to_title(collection),
        properties=properties,
    )

    # Append the new collection to Sync Activity's `collection` select so
    # conflict rows for this collection can be recorded. Best-effort: log
    # but don't fail the whole provision if this PATCH errors — the caller
    # can retry from settings.
    try:
        add_collection_to_sync_activity(nc, collection=collection)
    except Exception as e:
        print(f"[provisioner] add_collection_to_sync_activity failed: {e}")

    return db["id"]


def add_collection_to_sync_activity(
    nc: NotionClient, *, collection: str
) -> None:
    """PATCH the Sync Activity DB to include `collection` as a select option.
    Idempotent — re-adding an existing option is a no-op."""
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
    """Fetch a single collection's full definition (fields, indexes, etc)."""
    return pb._http("GET", f"/api/collections/{name}")  # noqa: SLF001
```

### 5.3 PB→Notion property mapping (the authoritative table)

Implement `_pb_field_to_notion_property_definition` as a pure function
on the **PB field spec dict** (the shape PB returns from
`GET /api/collections/<name>`). Return `None` when the column should be
skipped (currently only "relation to an un-synced target" — see 5.5).

| PB field type | PB extras | Notion property body |
|---|---|---|
| `text`, `editor`, `autodate` | — | `{"rich_text": {}}` |
| `password` | — | **Skip** (return None — never sync passwords) |
| `number` | — | `{"number": {"format": "number"}}` |
| `bool`   | — | `{"checkbox": {}}` |
| `email`  | — | `{"email": {}}` |
| `url`    | — | `{"url": {}}` |
| `date`   | — | `{"date": {}}` |
| `select` | `maxSelect == 1` | `{"select": {"options": [{"name": v} for v in field["values"]]}}` |
| `select` | `maxSelect > 1` | `{"multi_select": {"options": [{"name": v} for v in field["values"]]}}` |
| `relation` | target also synced and enabled | `{"relation": {"database_id": "<target.notion_db_id>", "single_property": {}}}` |
| `relation` | target NOT synced (or disabled) | **Skip** (return None). Log via `print(f"[provisioner] skipping relation field {name!r} — target {target_name!r} is not synced")` |
| `json`   | — | `{"rich_text": {}}` (already how codec round-trips JSON) |
| `file`   | — | `{"files": {}}` |
| any other unknown type | — | `{"rich_text": {}}` (safe fallback; log a warning) |

The PB field dict for a relation looks like
`{"name": "day", "type": "relation", "collectionId": "<pb-collection-id>", "maxSelect": 1}`.
To resolve `collectionId` → PB collection name, call
`pb._http("GET", f"/api/collections/{collectionId}")` inside the helper
and read `[\"name\"]`. Then look up that collection name in
`all_targets`; if found AND `enabled`, use its `notion_db_id`. If not
found, return None.

### 5.4 Sync Activity `collection` select option — added inside provision_notion_db

See `add_collection_to_sync_activity` in the API above. It is called
from `provision_notion_db` after `nc.create_database` succeeds and
before returning. Wrapped in try/except so a failure here does not abort
the whole provision (the caller logs and the user can retry from
settings).

### 5.5 Relation handling — exact rules

Decision: target also synced → `relation`; target NOT synced → skip.

"Target also synced" means: at the moment `provision_notion_db` runs,
there is a `sync_config` row for the target collection AND its `enabled`
is true. `load_all(pb, fresh=True)` is used so we see the row that the
caller may have *just* inserted in the same request.

Skipped columns leave their data in PB but invisible in Notion. The
existing codec/runner already tolerates "Notion property doesn't exist"
(see how `runner.py` reads `notion_schema = notion_db.get("properties", {})`
and `pb_record_to_notion_props` filters by schema), so no other code
needs to change. When the user later enables sync for the target, the
relation column will NOT be auto-added; that "alter Notion DB" pass is
out of scope (§13).

---

## 6. Components F / G / H — refactor existing code to use the loader

### 6.1 `notion_sync/runner.py`

**Delete** lines 55–58 (the `TITLE_FIELD_BY_COLLECTION` dict).

**Replace** the line in `sync_collection`:

```python
title_field = TITLE_FIELD_BY_COLLECTION.get(collection, "title")
```

with:

```python
title_field = cfg_row.get("title_field") or ""
if not title_field:
    raise RuntimeError(
        f"sync_config[{collection}].title_field is empty — set it via "
        f"the settings UI or PB admin before this collection can sync"
    )
```

`cfg_row` is the dict-shape PB row; after migration A it carries the
new `title_field` column. No `notion_sync.config` import is needed
inside `sync_collection` — the row IS the loader's source data.

`runner.main()` already calls
`pb.list_records("sync_config", filter="enabled=true", sort="")` which
returns rows with `title_field` populated. No further edit needed.

### 6.2 `scripts/reconcile_initial.py`

**Delete** lines 42–51 (the two hardcoded dicts).

**Change** `reconcile_one`'s signature to accept the resolved fields
from the row:

```python
def reconcile_one(collection: str, notion_db_id: str,
                  overrides: dict[str, str],
                  title_field: str,
                  date_field: str,
                  pb: PBClient, nc: NotionClient,
                  dry_run: bool) -> dict:
    print(f"\n=== {collection} ===")
    overrides_inv = {v: k for k, v in overrides.items()}
    field_types = collection_field_types(pb, collection)
    # title_field / date_field come from sync_config; no local dict.
```

And in `main()`:

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

### 6.3 `pb_tools.py`

**Delete** lines 51–53 (the `_AUTO_SYNC_COLLECTIONS` set literal).

**Change** `_schedule_auto_sync`:

```python
from notion_sync.config import collections_with_auto_sync

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

The `try/except` around `collections_with_auto_sync()` is important —
this code path runs inside the chat tool handler; we never want a PB
hiccup to take down the chat session.

Also update the `PROMPT_HINT` text (currently around lines 665–672) —
change the list "trips / days / stops / locations / todos / journal" to
"the collections marked `auto_sync` in sync_config (currently trips /
days / stops / locations / todos / journal — visible in the 同步设置
page)" so the chat agent knows it's runtime-driven.

---

## 7. Component I — new REST endpoints

All new endpoints live in `server.py`. They sit alongside the existing
`/api/sync/now`, `GET /api/sync/settings`, `PATCH /api/sync/settings`
endpoints (around line 1488–1577 in current main). Use **the same**
`PBClient()` pattern those endpoints already use (see `_pb_sync_global`
helper at line 1520 for the shape), and **always** call
`notion_sync.config.invalidate()` after a write.

### 7.1 `GET /api/sync/targets`

Lists all configured sync targets, plus PB collections not yet
configured (so the UI can show "available to enable").

Response body:

```json
{
  "configured": [
    {
      "id": "abc123...",
      "collection": "trips",
      "notion_db_id": "df7ea062-...",
      "enabled": true,
      "auto_sync": true,
      "title_field": "title",
      "date_field": "date_start",
      "field_map_overrides": {},
      "last_synced_at": "2026-06-04 03:00:01.123Z",
      "last_sync_summary": "runner: applied=2 conflicts=0 deletes=0"
    }
  ],
  "available": [
    {
      "collection": "ideas",
      "fields": [
        {"name": "title",  "type": "text",   "required": true},
        {"name": "status", "type": "select", "values": ["Open", "Done"]}
      ]
    }
  ]
}
```

`available` = `pb.list_collections()` with `type == "base"`, filtered
to **collections that have no `sync_config` row AND are not in this
fixed system blocklist**: `sync_config`, `sync_global`,
`_pb_users_auth_`, `_superusers`, `_mfas`, `_otps`, `_authOrigins`,
`_externalAuths`. (The leading-underscore ones are always there but PB
hides them from non-admin REST; defensively skip anyway.)

Each entry in `available.fields` is the field spec verbatim from
`pb_list_collections`, so the UI can suggest sensible `title_field`
defaults (prefer `title`, then `name`, then the first `text+required`
field).

### 7.2 `POST /api/sync/targets`

Create a new sync target end-to-end. **This is the heavy one.**

Request body:

```json
{
  "collection":  "ideas",
  "title_field": "title",
  "date_field":  "",
  "auto_sync":   true
}
```

Server flow (in order):

1. **Validate** `collection` exists in PB, `title_field` is one of its
   fields. Reject with 400 if not.
2. **Check** there is no existing `sync_config` row for this collection.
   Reject with 409 if there is.
3. **Provision** the Notion DB:
   `notion_db_id = provisioner.provision_notion_db(pb=…, nc=…, collection=…, title_field=…)`
4. **Insert** the `sync_config` row:
   ```python
   pb.create_record("sync_config", {
       "collection": collection,
       "notion_db_id": notion_db_id,
       "enabled": True,
       "auto_sync": auto_sync,
       "title_field": title_field,
       "date_field": date_field,
       "field_map_overrides": {},
   })
   ```
5. **Invalidate** the registry cache:
   `notion_sync.config.invalidate()`.
6. **Spawn** an async reconcile in the background:
   ```python
   asyncio.create_task(_spawn_reconcile_initial(collection))
   ```
   Where `_spawn_reconcile_initial` runs:
   ```
   /home/dev/phone-bridge/.venv/bin/python scripts/reconcile_initial.py --only <collection>
   ```
   Mirror the subprocess pattern in `pb_tools._run_debounced_sync`. Do
   not await — the HTTP response should return as soon as the row
   exists.
7. **Return**:
   ```json
   { "ok": true, "notion_db_id": "...", "reconcile_started": true }
   ```

If any step **before** step 4 fails, return the error. If the row was
inserted (step 4 succeeded) but a later step fails, return
`{"ok": true, "notion_db_id": "...", "post_setup_error": "..."}` — the
target IS registered and re-running the post-setup steps is safe.

### 7.3 `PATCH /api/sync/targets/{collection}`

Patch a single sync_config row. Accepts a JSON body with any subset of:

| Key | Type | Notes |
|---|---|---|
| `enabled` | bool | |
| `auto_sync` | bool | |
| `title_field` | string | Must be a real field on the PB collection. |
| `date_field` | string (may be empty) | If non-empty, must be a real field. |
| `field_map_overrides` | object<string, string> | Replaces the whole dict. |

Server flow:

1. Look up the row via `pb.list_records("sync_config", filter=f"collection='{c}'", sort="")`. 404 if missing.
2. Validate any new `title_field` / `date_field` exists on the PB collection.
3. PATCH the row with only the provided keys.
4. Invalidate cache.
5. Return the updated row as JSON.

### 7.4 `DELETE /api/sync/targets/{collection}`

Unsync a collection. Per the user's decision, the Notion DB is **kept**.

Server flow:

1. Look up the row. 404 if missing.
2. Delete the row: `pb.delete_record("sync_config", row["id"])`.
3. Invalidate cache.
4. Return `{"ok": true, "notion_db_id": "<kept>"}` so the UI can show
   the user the URL of the orphaned DB if they want to archive it
   themselves.

The Sync Activity `collection` select option is **left in place** (Notion
doesn't support removing a select option that has rows, and removing it
from the schema only would orphan historical Pending rows).

### 7.5 `POST /api/sync/registry/export-snapshot`

Trigger the snapshot dump (§9). Returns:

```json
{ "ok": true, "path": "notion_sync/registry.snapshot.yaml" }
```

This is the only programmatic path that runs the script; the user can
also run it from shell. The endpoint shells out to the same script as
the CLI (`python scripts/dump_sync_registry.py`) so there is exactly
one implementation.

---

## 8. Component J — settings UI extension

### 8.1 Where it goes

The existing "同步设置" modal is opened from
`static/index.html` line 100 (`<button data-cmd="sync-settings">`) and
handled in `static/app.js` line 1504 (`else if (cmd === 'sync-settings')`).

Today it shows timezone + 2 hour pickers + paused toggle. **Add a new
section below**: "同步表" listing the result of `GET /api/sync/targets`.

### 8.2 Visual layout

```
┌─ 同步设置 ────────────────────────────┐
│ 时区:  [America/New_York ▾]            │
│ 同步时间: [03] 和 [15]                  │
│ 暂停:   ☐                              │
│                                         │
│ ── 同步表 ───────────────────────────  │
│ trips     ☑启用 ☑自动 [title][date_..] │
│ days      ☑启用 ☑自动 [name ][date  ] │
│ stops     ☑启用 ☑自动 [name ][date  ] │
│ ...                                     │
│                                         │
│ [+ 新增同步表]                         │
│                                         │
│ [取消]  [保存]                         │
└────────────────────────────────────────┘
```

When the user clicks **+ 新增同步表**, a sub-section / inner dialog
appears:

```
┌─ 新增同步 ───────────────────────────┐
│ PB 集合: [ideas ▾]                    │
│ Notion 标题列 (title_field): [title ▾]│
│ 日期列 (date_field, 可选):    [— ▾]  │
│ 自动同步:                     ☑       │
│                                       │
│ [取消]  [创建并同步]                  │
└──────────────────────────────────────┘
```

- "PB 集合" dropdown populated from `available` in the GET response.
- "Notion 标题列" dropdown shows that collection's fields; default to
  the first of `title`, `name`, or first required text field.
- "日期列" dropdown shows that collection's `date`-type fields plus `—` (empty).

On submit → `POST /api/sync/targets` with the form values. On success,
re-fetch `GET /api/sync/targets` and re-render the table. Show a toast
saying "已创建,后台正在做首次对齐".

### 8.3 Per-row controls

For an existing row in the "同步表" table:
- Toggle `enabled` → PATCH `{enabled: bool}`
- Toggle `auto_sync` → PATCH `{auto_sync: bool}`
- Click `title_field` / `date_field` → inline edit (dropdown of fields), PATCH on commit
- Click ✕ at the row's right → confirm modal "停止同步 `<collection>`?
  Notion DB will be kept." → DELETE

### 8.4 No-op safety

If `GET /api/sync/targets` returns a 5xx, render an error band at the
top of the modal ("同步配置读取失败: <message>. 已有定时同步仍按上次配置运行")
instead of breaking the existing timezone/paused controls.

### 8.5 Code locations to edit

- `static/index.html`: add a `<section id="sync-targets-section">` inside
  the existing sync-settings modal. Add a `<dialog id="sync-add-dialog">`
  next to `<dialog id="checkin-dialog">`.
- `static/app.js`: add `loadSyncTargets()`, `renderSyncTargets()`,
  `openAddSyncTarget()`, `submitAddSyncTarget()`,
  `patchSyncTarget(collection, patch)`, `deleteSyncTarget(collection)`.
  Hook into the existing `'sync-settings'` cmd handler.
- `static/style.css`: minor styles for the targets table + add button.

Keep the new code under ~250 lines of `app.js` — same shape as the
existing `openSyncSettings()` helper.

---

## 9. Component D — `scripts/dump_sync_registry.py`

### 9.1 Behavior

Reads PB `sync_config` + `sync_global` and writes a **human-readable
YAML** snapshot to `notion_sync/registry.snapshot.yaml`. Idempotent;
overwrites the file each run.

### 9.2 Output format

```yaml
# Auto-generated by scripts/dump_sync_registry.py.
# Source of truth: PB sync_config / sync_global tables.
# Regenerate after every UI change: python scripts/dump_sync_registry.py
# and commit the diff so disaster-recovery git can rebuild PB.

generated_at: "2026-06-04T12:00:00Z"
sync_global:
  timezone: America/New_York
  sync_hour_local: 3
  sync_hour_local_2: 15
  paused: false
  last_run_at: "2026-06-04 03:00:08.412Z"
sync_targets:
  - collection: trips
    notion_db_id: df7ea062-7b18-4c4f-98f1-bfec8258c3db
    enabled: true
    auto_sync: true
    title_field: title
    date_field: date_start
    field_map_overrides: {}
    last_synced_at: "2026-06-04 03:00:01.123Z"
    last_sync_summary: "runner: applied=2 conflicts=0 deletes=0"
  - collection: days
    # ...
```

Decision: do **not** add PyYAML to `requirements.txt`. The output shape
is fixed and shallow; write it with a small hand-rolled emitter
(~40 lines). The emitter only needs to handle: scalars (str/int/bool/null),
empty dict `{}`, ISO datetime strings, and one level of nesting. Quote
strings that contain `:` or start with whitespace; leave others bare.

### 9.3 CLI

```
python scripts/dump_sync_registry.py             # writes notion_sync/registry.snapshot.yaml
python scripts/dump_sync_registry.py --stdout    # prints to stdout
python scripts/dump_sync_registry.py --path X.yaml   # custom output
```

Exit 0 on success, 1 on failure (PB unreachable etc.).

### 9.4 What it does NOT do

- It does NOT push to git. The user commits the diff themselves.
- It does NOT read from Notion. The snapshot is PB-shaped.
- It does NOT include credentials. The only "external" id it persists is
  `notion_db_id` — already present in `scripts/setup_notion_sync_db.py`
  and committed to git. Safe to commit.

---

## 10. Component K — documentation updates

Edit, in this order:

1. **`CLAUDE.md`** — add a new section under "Notion sync" titled
   "Sync registry (where the list of synced tables lives)" with a
   3-paragraph summary pointing readers to this spec and to the
   settings UI. Mention `scripts/dump_sync_registry.py`.

2. **`docs/notion-pb-sync.md`** — update the section on "Adding a new
   sync target" (or create it if absent) to describe the new UI-driven
   flow. The old "edit 4 files" description should be deleted.

3. **`docs/data-model.md`** — under the `sync_config` row, add the
   three new columns (`title_field`, `date_field`, `auto_sync`) with
   their seed values and the meaning of each.

4. **`scripts/setup_notion_sync_db.py`** — expand the existing "NOT
   THE SOURCE OF TRUTH" comment block on `SYNC_TARGETS` to add:
   "After 2026-06-04 the per-target metadata (title_field, date_field,
   auto_sync) lives in extra columns on sync_config. This bootstrap
   script does NOT seed those — the migration `1779465623_extend_sync_config.js`
   does."

No new top-level README is needed.

---

## 11. Tests

### 11.1 `tests/notion_sync/test_config.py` (new)

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

### 11.2 `tests/notion_sync/test_provisioner.py` (new)

Test cases (all using fakes for `PBClient` / `NotionClient` — do not hit
real services):

| Case | Asserts |
|---|---|
| basic text collection (1 title, 2 text fields) | properties dict has Title + 2 rich_text + pb_id + last_synced_at |
| collection with select (maxSelect=1, 3 values) | property is `{"select": {"options": [{"name": v} for v in values]}}` |
| collection with multi-select (maxSelect=3) | property is `multi_select` |
| relation to a synced target | property is `{"relation": {"database_id": "...", "single_property": {}}}` |
| relation to an unsynced target | property is **omitted** from the dict |
| title_field not on the collection | `RuntimeError` |
| collection name not found | `RuntimeError` from `_get_collection` |
| sync_activity option added | confirm `nc.update_database` called with new option appended |
| password field | **omitted** from the dict |

### 11.3 Adjust existing tests

`tests/notion_sync/test_runner_guard.py` already covers the time-guard.
After this change, the dict-shape `cfg_row` test fixtures must include
`title_field: "title"`. Add that key to existing fixtures; no other
runner test changes.

### 11.4 Manual test plan (after deploy)

1. Open settings → confirm 8 existing targets render with their seeded values.
2. Toggle `auto_sync` on `plans` → run `pb_create` against plans → confirm runner does NOT fire (auto_sync was false). Toggle on → run again → runner fires.
3. Create a brand-new test collection via `pb_create_collection` in chat. Verify it appears under "available" in settings. Enable sync. Verify:
   - New Notion DB visible under the sync parent page
   - DB has `pb_id`, `last_synced_at`, and one column per non-system PB field
   - Sync Activity DB's `collection` select has the new entry
   - `reconcile_initial` ran (check `.bridge_data/sync.log`)
   - sync_config has a new row
4. Delete the test sync target via settings. Confirm Notion DB still exists and is untouched.
5. `python scripts/dump_sync_registry.py` → check `notion_sync/registry.snapshot.yaml` has expected content; `git diff` it.

---

## 12. Rollout sequence

Implementation order (each step compiles + tests cleanly on its own):

1. **Component A** (migration). Deploy. Verify in PB admin that 3 new
   columns exist + 8 rows are seeded.
2. **Component B** (`notion_sync/config.py`) + tests. Run unit tests.
   Does not yet refactor consumers.
3. **Component F** (runner.py refactor — drop hardcoded dict, read from
   `cfg_row`). Deploy. Hit `/api/sync/now` and watch the runner pass
   for all 8 targets to confirm no regression.
4. **Component G** (reconcile_initial.py refactor). Run dry-run for one
   collection to confirm signature change works.
5. **Component H** (pb_tools.py — switch to `collections_with_auto_sync`).
   Deploy. Issue an MCP `pb_update` against `trips`, watch `.bridge_data/sync.log`
   for the debounced runner trigger. Issue one against `plans`, confirm
   it does NOT fire.
6. **Component C** (provisioner.py) + tests.
7. **Component I** (server.py REST endpoints).
8. **Component J** (UI). Test end-to-end: enable sync for a new
   collection from the phone.
9. **Component D** (snapshot script). Run it. Commit the YAML.
10. **Component K** (docs).

Each step is its own commit; sub-step 8 (UI) may be 2–3 commits.

---

## 13. Out of scope (NOT to be implemented in this work)

These have been **explicitly considered and rejected** by the user or by
this design. An implementation agent that adds them is going outside
the spec.

- **`sync_direction` field on sync_config** — sync stays bidirectional
  for every target.
- **`debounce_seconds` field per target** — stays globally 10s
  (constant `_AUTO_SYNC_DEBOUNCE_SECS` in pb_tools.py).
- **Per-column whitelists / blacklists** — granularity stays "open"
  per user's "保持开放式" choice.
- **Auto Notion DB schema re-sync when PB collection schema changes** —
  the provisioner only runs at creation. If you later add a PB field,
  you re-add the Notion property by hand (or via a future "alter"
  endpoint, out of scope).
- **Auto-creating PB collections from the UI** — the user explicitly
  retains the "chat with Claude → pb_create_collection" flow. The
  settings UI never creates PB collections.
- **Notion DB archive on unsync** — user chose "keep". A future
  "Archive Notion DB" button is fine, just not now.
- **Auto-export of the YAML snapshot on every config change** — user
  chose manual.
- **Adding PyYAML as a dependency** — keep the snapshot script
  self-contained.
- **MCP tools for the new settings (e.g. `sync_register`)** — the chat
  agent does not need them; the user uses the UI for this.

---

## 14. Hallucination guard — checklist for the implementing agent

Before considering this work done, verify each:

- [ ] Migration timestamp is `1779465623`, NOT `1779465624` or later.
- [ ] Migration body matches §3.3 verbatim (including the 8-entry SEED dict).
- [ ] `notion_sync/config.py` exports exactly: `SyncTarget`, `load_all`, `load_enabled`, `get`, `collections_with_auto_sync`, `invalidate`. No others.
- [ ] `notion_sync/runner.py` no longer contains the string `TITLE_FIELD_BY_COLLECTION`.
- [ ] `scripts/reconcile_initial.py` no longer contains `DATE_FIELD_BY_COLLECTION`.
- [ ] `pb_tools.py` no longer contains the literal set `{"trips", "days", "stops", "locations", "todos", "journal"}`.
- [ ] `notion_sync/provisioner.py` calls `nc.create_database` exactly once per `provision_notion_db` call.
- [ ] `provisioner` skips PB fields named in: `{id, created, updated, notion_id, notion_last_edited, last_synced_at, pb_id}`.
- [ ] `provisioner` skips PB fields of type `password`.
- [ ] `provisioner` skips relation fields whose target is not enabled in sync_config (returns None for that field).
- [ ] All five new REST endpoints exist: `GET /api/sync/targets`, `POST /api/sync/targets`, `PATCH /api/sync/targets/{c}`, `DELETE /api/sync/targets/{c}`, `POST /api/sync/registry/export-snapshot`.
- [ ] `POST /api/sync/targets` calls `notion_sync.config.invalidate()` after step 4.
- [ ] `DELETE` does NOT call `nc.update_page(db_id, archived=True)` on the Notion DB.
- [ ] Snapshot script does NOT add PyYAML to `requirements.txt`.
- [ ] `static/index.html` references the same `data-cmd="sync-settings"` button (line 100) — do not add a second menu item.
- [ ] All four UI verbs (enable toggle, auto_sync toggle, edit fields, delete) round-trip to the API.
- [ ] Test files exist at `tests/notion_sync/test_config.py` and `tests/notion_sync/test_provisioner.py`.

If any of the above is `not done` or `unsure`, the agent stops and asks
the user instead of guessing.

---

## 15. User's source quotes (verbatim)

These were captured during the brainstorming session and anchor the
decisions above:

> 现在的代码， 如果以后我要添加新的数据库， 也要同步，会需要改很多吗？如果都是写在代码里话，后面需要模块化

> 我们先来讨论， 就是逻辑先跑通， 需要一个库来记载每个表的结构，和需要同步的部分， 如果加新表后， 只需要在这个库里登记新的表格名称和结构， 然后列出需要同步用到的列，是不是就简单一些？

> 不用，保持开放式就好。

> 我也偏向于使用现有的pb ，但是我也担心耦合的问题。

> 还有， 后期我准备加一个设置页面， 可以选择哪些数据库同步，哪些不同步的， 所以，yaml文件比较合适？

> A+ — PB 为主 + 可导出 snapshot 到 git

> 一次性做全 — 连设置页面一起上

> 先不改代码，写个详细的规划书， 并且做到给了agent 不要产生幻觉的那种

> 我一般不会用notion 建表， 只会用pb建表，然后选择是否同步到notion上

> phone bridge那边我提要求，建立新的数据库给新的需求， 然后选择要不要同步notion ， 如果选择同步， 以后在这个数据库里添加的数据（pb里）就要同步到notion ， 如果notion里这个表有改动，也会进到对比库里等我确认

---

## 16. Change log

- 2026-06-04 — Initial design, brainstormed and approved.
