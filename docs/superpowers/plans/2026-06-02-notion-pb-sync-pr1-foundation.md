# Notion ↔ PB Sync — PR1: Foundation & Initial Reconciliation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the sync pipeline schema (PB + Notion sides), a Sync Activity Notion DB, and a one-shot `reconcile_initial.py` script that aligns the existing ~80%-overlapping data between PB and Notion. Output: every row on both sides has a matched ID, or a `Possible duplicate` entry in Sync Activity for the user to resolve. **Does NOT install any cron.**

**Architecture:** A new Python package `notion_sync/` provides the building blocks (PB client, Notion client, field codec, fuzzy matcher, backup, activity-log helpers). Two scripts use it: `setup_notion_sync_db.py` bootstraps Notion-side schema (adds pipeline columns + creates Sync Activity DB), and `reconcile_initial.py` does the one-shot data alignment. PB-side schema is added via two new migrations.

**Tech Stack:** Python 3.11+, stdlib `urllib.request` (matching `pb_tools.py` style — no new HTTP lib), Notion REST API, PocketBase REST API, `pytest` for tests.

**Spec reference:** `docs/superpowers/specs/2026-06-02-notion-pb-sync-design.md` — see "数据模型" and "初次对齐(PR1 一次性脚本)" sections.

---

## File Structure

**Created:**
- `pocketbase/pb_migrations/1779465616_create_sync_meta.js` — `sync_config` + `sync_global` PB collections
- `pocketbase/pb_migrations/1779465617_add_sync_pipeline_fields.js` — adds `notion_id` / `notion_last_edited` / `last_synced_at` to 6 sync-target collections
- `notion_sync/__init__.py` — package marker
- `notion_sync/pb_api.py` — minimal PB HTTP wrapper (auth + CRUD), reuses the pattern from `pb_tools.py`
- `notion_sync/notion_api.py` — minimal Notion REST wrapper (query DB, create/update/retrieve page, with 2 req/s rate limit)
- `notion_sync/codec.py` — PB ↔ Notion field-value conversion (text / number / date / select / relation / etc.)
- `notion_sync/matching.py` — fuzzy match (title + date) for reconcile
- `notion_sync/backup.py` — write a JSON snapshot of every PB collection to `.bridge_data/backups/<timestamp>/`
- `notion_sync/activity.py` — read/write helpers for the Sync Activity Notion DB
- `scripts/__init__.py` — package marker (if not present)
- `scripts/setup_notion_sync_db.py` — bootstrap: add pipeline columns to existing 6 Notion DBs, create Sync Activity DB, register everything in `sync_config`
- `scripts/reconcile_initial.py` — orchestrator: matches, links, fills gaps, queues duplicates
- `tests/notion_sync/__init__.py`
- `tests/notion_sync/test_codec.py`
- `tests/notion_sync/test_matching.py`
- `tests/notion_sync/test_backup.py`

**Modified:**
- `CLAUDE.md` — add a short section on the sync workflow (where backups live, how to re-run reconcile)

**No changes (yet):** `server.py`, `pb_tools.py`, `requirements.txt` — PR1 uses stdlib only and doesn't add any MCP tools or HTTP endpoints. Those land in PR2/PR3.

---

## Pre-Task Setup

- [ ] **Step 0: Verify environment**

Run:
```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && grep -E '^(NOTION_TOKEN|POCKETBASE_URL|POCKETBASE_ADMIN_EMAIL|POCKETBASE_ADMIN_PASSWORD)=' .env | sed 's/=.*/=<set>/'"
```

Expected: all 4 var names listed with `<set>`. If `NOTION_TOKEN` is missing, the user creates a Notion internal integration at https://www.notion.so/profile/integrations, connects it to the workspace, and adds the token to `.env`. The integration also needs to be "Add connections"-ed on each of the 6 sync target DBs (Notion's per-DB permission model).

---

## Task 1: PB Migration — `sync_config` + `sync_global` collections

**Files:**
- Create: `pocketbase/pb_migrations/1779465616_create_sync_meta.js`

PB migrations run automatically on next PocketBase start.

- [ ] **Step 1: Create the migration file**

```javascript
/// <reference path="../pb_data/types.d.ts" />
//
// Sync meta collections — per-collection config and global config for the
// Notion <-> PB sync pipeline. Created by PR1. Cron logic lands in PR2.
//
migrate((app) => {
  // sync_config — one row per collection that gets synced to Notion.
  const cfg = new Collection({
    name: "sync_config",
    type: "base",
    listRule: null, viewRule: null, createRule: null, updateRule: null, deleteRule: null,
    fields: [
      { name: "collection",            type: "text", required: true, max: 100 },
      { name: "notion_db_id",          type: "text", required: true, max: 100 },
      { name: "enabled",               type: "bool" },
      { name: "field_map_overrides",   type: "json", maxSize: 100000 },
      { name: "last_synced_at",        type: "date" },
      { name: "last_sync_summary",     type: "text", max: 1000 },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_sync_config_collection ON sync_config (collection)",
    ],
  });
  app.save(cfg);

  // sync_global — single-row global settings (timezone, sync hour, paused).
  const glb = new Collection({
    name: "sync_global",
    type: "base",
    listRule: null, viewRule: null, createRule: null, updateRule: null, deleteRule: null,
    fields: [
      { name: "timezone",        type: "text", required: true, max: 100 },
      { name: "sync_hour_local", type: "number", required: true },
      { name: "paused",          type: "bool" },
      { name: "last_run_at",     type: "date" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
  });
  app.save(glb);

  // Seed one sync_global row with sensible defaults.
  const initial = new Record(glb, {
    timezone: "America/New_York",
    sync_hour_local: 3,
    paused: false,
  });
  app.save(initial);
}, (app) => {
  for (const name of ["sync_config", "sync_global"]) {
    try { app.delete(app.findCollectionByNameOrId(name)); } catch (e) {}
  }
});
```

- [ ] **Step 2: Apply the migration**

Run `deploy` from the project root. The deploy script restarts phone-bridge; PocketBase picks up new migration files on its own restart.

Verify:
```powershell
ssh dashboard-server 'curl -s -X POST http://127.0.0.1:8090/api/collections/_superusers/auth-with-password -H "Content-Type: application/json" -d "{\"identity\":\"$POCKETBASE_ADMIN_EMAIL\",\"password\":\"$POCKETBASE_ADMIN_PASSWORD\"}" | jq -r .token > /tmp/pb_tok && curl -s -H "Authorization: $(cat /tmp/pb_tok)" http://127.0.0.1:8090/api/collections | jq -r ".items[].name | select(startswith(\"sync_\"))"'
```
Expected: prints `sync_config` and `sync_global` (order may vary).

- [ ] **Step 3: Commit**

```powershell
git add pocketbase/pb_migrations/1779465616_create_sync_meta.js
git commit -m "PR1: add sync_config + sync_global PB collections"
```

---

## Task 2: PB Migration — Add pipeline fields to 6 sync-target collections

**Files:**
- Create: `pocketbase/pb_migrations/1779465617_add_sync_pipeline_fields.js`

Adds `notion_id` / `notion_last_edited` / `last_synced_at` to `trips`, `days`, `plans`, `todos`, `contacts`, `locations`. Idempotent — checks each field before adding.

- [ ] **Step 1: Create the migration file**

```javascript
/// <reference path="../pb_data/types.d.ts" />
//
// Add Notion sync pipeline fields to the 6 collections that will be synced
// to Notion. Idempotent: skips fields that already exist (so re-running on
// a partially-migrated DB is safe).
//
const SYNC_TARGETS = ["trips", "days", "plans", "todos", "contacts", "locations"];

const PIPELINE_FIELDS = [
  { name: "notion_id",          type: "text", max: 100 },
  { name: "notion_last_edited", type: "date" },
  { name: "last_synced_at",     type: "date" },
];

migrate((app) => {
  for (const name of SYNC_TARGETS) {
    const c = app.findCollectionByNameOrId(name);
    const existing = new Set(c.fields.map((f) => f.name));
    let touched = false;
    for (const spec of PIPELINE_FIELDS) {
      if (existing.has(spec.name)) continue;
      c.fields.push(new Field(spec));
      touched = true;
    }
    const idxName = `idx_${name}_notion_id`;
    if (!c.indexes.some((s) => s.includes(idxName))) {
      c.indexes.push(`CREATE UNIQUE INDEX ${idxName} ON ${name} (notion_id) WHERE notion_id != ''`);
      touched = true;
    }
    if (touched) app.save(c);
  }
}, (app) => {
  for (const name of SYNC_TARGETS) {
    try {
      const c = app.findCollectionByNameOrId(name);
      c.fields = c.fields.filter((f) => !["notion_id", "notion_last_edited", "last_synced_at"].includes(f.name));
      c.indexes = c.indexes.filter((s) => !s.includes(`idx_${name}_notion_id`));
      app.save(c);
    } catch (e) {}
  }
});
```

- [ ] **Step 2: Apply + verify**

Run `deploy`, then:
```powershell
ssh dashboard-server 'TOKEN=$(cat /tmp/pb_tok); for c in trips todos contacts; do echo "== $c =="; curl -s -H "Authorization: $TOKEN" "http://127.0.0.1:8090/api/collections/$c" | jq -r ".fields[].name" | grep -E "notion_id|notion_last_edited|last_synced_at"; done'
```
Expected: all 3 field names print under each of `trips`, `todos`, `contacts`.

- [ ] **Step 3: Commit**

```powershell
git add pocketbase/pb_migrations/1779465617_add_sync_pipeline_fields.js
git commit -m "PR1: add notion_id/notion_last_edited/last_synced_at to 6 sync targets"
```

---

## Task 3: Package skeleton

**Files:**
- Create: `notion_sync/__init__.py`
- Create: `tests/notion_sync/__init__.py`

- [ ] **Step 1: Create the package markers**

`notion_sync/__init__.py`:
```python
"""Notion ↔ PocketBase sync package.

PR1 contents: pb_api, notion_api, codec, matching, backup, activity helpers.
PR2 adds: the cron-driven sync runner.
PR3 adds: MCP tools + push notifier.
"""
```

`tests/notion_sync/__init__.py`:
```python
```

- [ ] **Step 2: Commit**

```powershell
git add notion_sync/__init__.py tests/notion_sync/__init__.py
git commit -m "PR1: notion_sync package skeleton"
```

---

## Task 4: `notion_sync/pb_api.py` — PB HTTP wrapper

**Files:**
- Create: `notion_sync/pb_api.py`

Reuses the auth-and-HTTP pattern from `pb_tools.py` but as a plain sync class.

- [ ] **Step 1: Write the implementation**

```python
"""Sync wrapper around PocketBase REST API.

Mirrors the auth pattern from `pb_tools.py` but as plain blocking functions
suitable for scripts (no async / MCP scaffolding).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class PBClient:
    def __init__(self,
                 url: str | None = None,
                 email: str | None = None,
                 password: str | None = None) -> None:
        self.url = (url or os.environ["POCKETBASE_URL"]).rstrip("/")
        self.email = email or os.environ["POCKETBASE_ADMIN_EMAIL"]
        self.password = password or os.environ["POCKETBASE_ADMIN_PASSWORD"]
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def _http(self, method: str, path: str, body: Any | None = None,
              authed: bool = True) -> Any:
        url = f"{self.url}{path}"
        headers = {"Content-Type": "application/json"}
        if authed:
            headers["Authorization"] = self._auth()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30.0) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"PB {method} {path}: {e.code} {raw[:500]}") from None

    def _auth(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        data = self._http("POST",
                          "/api/collections/_superusers/auth-with-password",
                          body={"identity": self.email, "password": self.password},
                          authed=False)
        self._token = data["token"]
        self._token_expiry = time.time() + 25 * 60
        return self._token

    def list_records(self, collection: str, *,
                     filter: str = "", sort: str = "-created",
                     per_page: int = 200) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            params = [f"page={page}", f"perPage={per_page}",
                      f"sort={urllib.parse.quote(sort, safe=',-')}"]
            if filter:
                params.append("filter=" + urllib.parse.quote(filter, safe=""))
            data = self._http("GET",
                              f"/api/collections/{collection}/records?" + "&".join(params))
            items = data.get("items", [])
            out.extend(items)
            if page >= data.get("totalPages", 1):
                break
            page += 1
        return out

    def get_record(self, collection: str, record_id: str) -> dict:
        return self._http("GET", f"/api/collections/{collection}/records/{record_id}")

    def create_record(self, collection: str, data: dict) -> dict:
        return self._http("POST", f"/api/collections/{collection}/records", body=data)

    def update_record(self, collection: str, record_id: str, data: dict) -> dict:
        return self._http("PATCH",
                          f"/api/collections/{collection}/records/{record_id}",
                          body=data)

    def delete_record(self, collection: str, record_id: str) -> None:
        self._http("DELETE", f"/api/collections/{collection}/records/{record_id}")

    def list_collections(self) -> list[dict]:
        return self._http("GET", "/api/collections?perPage=200").get("items", [])
```

- [ ] **Step 2: Smoke test on the VM**

```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && .venv/bin/python -c 'from notion_sync.pb_api import PBClient; c = PBClient(); cs = sorted(x[\"name\"] for x in c.list_collections()); print(cs)'"
```
Expected: list including `trips`, `todos`, `sync_config`, `sync_global`.

- [ ] **Step 3: Commit**

```powershell
git add notion_sync/pb_api.py
git commit -m "PR1: notion_sync.pb_api — sync PocketBase HTTP wrapper"
```

---

## Task 5: `notion_sync/notion_api.py` — Notion HTTP wrapper

**Files:**
- Create: `notion_sync/notion_api.py`

Stdlib only. Rate-limits to 2 req/s.

- [ ] **Step 1: Write the implementation**

```python
"""Sync wrapper around Notion REST API.

Stdlib urllib only. Rate-limits to 2 req/s globally so we stay under
Notion's 3 req/s burst limit.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

NOTION_API_VERSION = "2022-06-28"


class NotionClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ["NOTION_TOKEN"]
        self._rate_lock = threading.Lock()
        self._last_call_at: float = 0.0

    def _throttle(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            wait = 0.5 - (now - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    def _http(self, method: str, path: str, body: Any | None = None) -> Any:
        self._throttle()
        url = f"https://api.notion.com/v1{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30.0) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"Notion {method} {path}: {e.code} {raw[:500]}") from None

    def query_database(self, database_id: str, *,
                       filter_: dict | None = None,
                       sorts: list[dict] | None = None,
                       page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        start_cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if filter_: body["filter"] = filter_
            if sorts: body["sorts"] = sorts
            if start_cursor: body["start_cursor"] = start_cursor
            data = self._http("POST", f"/databases/{database_id}/query", body=body)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")
        return out

    def retrieve_database(self, database_id: str) -> dict:
        return self._http("GET", f"/databases/{database_id}")

    def update_database(self, database_id: str, body: dict) -> dict:
        return self._http("PATCH", f"/databases/{database_id}", body=body)

    def create_database(self, parent_page_id: str, title: str,
                        properties: dict) -> dict:
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        return self._http("POST", "/databases", body=body)

    def retrieve_page(self, page_id: str) -> dict:
        return self._http("GET", f"/pages/{page_id}")

    def create_page(self, database_id: str, properties: dict) -> dict:
        return self._http("POST", "/pages", body={
            "parent": {"database_id": database_id},
            "properties": properties,
        })

    def update_page(self, page_id: str, properties: dict | None = None,
                    archived: bool | None = None) -> dict:
        body: dict[str, Any] = {}
        if properties is not None: body["properties"] = properties
        if archived is not None: body["archived"] = archived
        return self._http("PATCH", f"/pages/{page_id}", body=body)
```

- [ ] **Step 2: Smoke test**

```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && .venv/bin/python -c 'from notion_sync.notion_api import NotionClient; c = NotionClient(); db = c.retrieve_database(\"df7ea062-7b18-4c4f-98f1-bfec8258c3db\"); print(db[\"title\"][0][\"plain_text\"])'"
```
Expected: prints the title of the trips Notion DB. If `404` → the integration isn't connected to that page; user clicks the `…` menu on the Notion DB page → "Add connections" → picks the integration.

- [ ] **Step 3: Commit**

```powershell
git add notion_sync/notion_api.py
git commit -m "PR1: notion_sync.notion_api — sync Notion REST wrapper with rate limit"
```

---

## Task 6: `notion_sync/codec.py` — PB ↔ Notion field conversion (TDD)

**Files:**
- Create: `notion_sync/codec.py`
- Create: `tests/notion_sync/test_codec.py`

- [ ] **Step 1: Write failing tests**

`tests/notion_sync/test_codec.py`:
```python
"""Codec round-trip and edge-case tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.codec import (
    snake_to_title,
    title_to_snake,
    pb_field_to_notion_property,
    notion_property_to_pb_field,
)


def test_snake_to_title_basic():
    assert snake_to_title("departure_time") == "Departure Time"
    assert snake_to_title("name") == "Name"
    assert snake_to_title("date_start") == "Date Start"


def test_title_to_snake_basic():
    assert title_to_snake("Departure Time") == "departure_time"
    assert title_to_snake("Name") == "name"
    assert title_to_snake("Date Start") == "date_start"


def test_pb_text_to_notion_rich_text():
    out = pb_field_to_notion_property("hello", pb_type="text")
    assert out == {"rich_text": [{"type": "text", "text": {"content": "hello"}}]}


def test_pb_number_to_notion():
    assert pb_field_to_notion_property(42, pb_type="number") == {"number": 42}
    assert pb_field_to_notion_property(None, pb_type="number") == {"number": None}


def test_pb_bool_to_notion_checkbox():
    assert pb_field_to_notion_property(True, pb_type="bool") == {"checkbox": True}


def test_pb_date_to_notion():
    out = pb_field_to_notion_property("2026-06-15", pb_type="date")
    assert out == {"date": {"start": "2026-06-15"}}


def test_pb_datetime_to_notion():
    out = pb_field_to_notion_property("2026-06-15 09:00:00.000Z", pb_type="date")
    assert out["date"]["start"].startswith("2026-06-15")


def test_pb_select_single_to_notion():
    out = pb_field_to_notion_property("Done", pb_type="select", max_select=1)
    assert out == {"select": {"name": "Done"}}


def test_pb_select_multi_to_notion():
    out = pb_field_to_notion_property(["A", "B"], pb_type="select", max_select=5)
    assert out == {"multi_select": [{"name": "A"}, {"name": "B"}]}


def test_pb_empty_text_to_notion_empty():
    out = pb_field_to_notion_property("", pb_type="text")
    assert out == {"rich_text": []}


def test_notion_rich_text_to_pb():
    notion_prop = {"type": "rich_text",
                   "rich_text": [{"plain_text": "hello"}, {"plain_text": " world"}]}
    assert notion_property_to_pb_field(notion_prop, pb_type="text") == "hello world"


def test_notion_title_to_pb():
    notion_prop = {"type": "title",
                   "title": [{"plain_text": "Trip to Paris"}]}
    assert notion_property_to_pb_field(notion_prop, pb_type="text") == "Trip to Paris"


def test_notion_number_to_pb():
    assert notion_property_to_pb_field({"type": "number", "number": 42}, pb_type="number") == 42
    assert notion_property_to_pb_field({"type": "number", "number": None}, pb_type="number") is None


def test_notion_checkbox_to_pb():
    assert notion_property_to_pb_field({"type": "checkbox", "checkbox": True}, pb_type="bool") is True


def test_notion_date_to_pb():
    notion_prop = {"type": "date", "date": {"start": "2026-06-15"}}
    assert notion_property_to_pb_field(notion_prop, pb_type="date") == "2026-06-15"


def test_notion_date_none_to_pb():
    assert notion_property_to_pb_field({"type": "date", "date": None}, pb_type="date") == ""


def test_notion_select_to_pb():
    notion_prop = {"type": "select", "select": {"name": "Done"}}
    assert notion_property_to_pb_field(notion_prop, pb_type="select", max_select=1) == "Done"


def test_notion_multi_select_to_pb():
    notion_prop = {"type": "multi_select",
                   "multi_select": [{"name": "A"}, {"name": "B"}]}
    assert notion_property_to_pb_field(notion_prop, pb_type="select", max_select=5) == ["A", "B"]


def test_roundtrip_text():
    pb_val = "departure at 09:00"
    notion = pb_field_to_notion_property(pb_val, pb_type="text")
    notion_resp = {"type": "rich_text", **notion}
    back = notion_property_to_pb_field(notion_resp, pb_type="text")
    assert back == pb_val


def test_roundtrip_multi_select():
    pb_val = ["X", "Y", "Z"]
    notion = pb_field_to_notion_property(pb_val, pb_type="select", max_select=5)
    notion_resp = {"type": "multi_select", **notion}
    back = notion_property_to_pb_field(notion_resp, pb_type="select", max_select=5)
    assert back == pb_val
```

- [ ] **Step 2: Run — verify they fail**

```powershell
python -m pytest tests/notion_sync/test_codec.py -v
```
Expected: `ModuleNotFoundError: No module named 'notion_sync.codec'`.

- [ ] **Step 3: Write the implementation**

`notion_sync/codec.py`:
```python
"""PB ↔ Notion field-value conversion.

Field-name handling:
  - PB uses snake_case (`departure_time`).
  - Notion uses Title Case display names (`Departure Time`).
  - Automatic two-way mapping; special cases live in `field_map_overrides`
    on the sync_config row.

Value handling: PB stores flat JSON; Notion wraps each property in a typed
envelope. This module converts both ways. PB-side type comes from the
collection field spec (caller looks it up via list_collections()).
"""
from __future__ import annotations

from typing import Any


def snake_to_title(name: str) -> str:
    """departure_time -> Departure Time"""
    return " ".join(word.capitalize() for word in name.split("_"))


def title_to_snake(name: str) -> str:
    """Departure Time -> departure_time"""
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def pb_field_to_notion_property(value: Any, *,
                                pb_type: str,
                                max_select: int = 1) -> dict:
    """Convert a PB value to the body of a Notion property update."""
    if pb_type in ("text", "editor", "email", "url"):
        s = str(value or "")
        if pb_type == "email":
            return {"email": s or None}
        if pb_type == "url":
            return {"url": s or None}
        if not s:
            return {"rich_text": []}
        return {"rich_text": [{"type": "text", "text": {"content": s}}]}

    if pb_type == "number":
        return {"number": value if value is not None else None}

    if pb_type == "bool":
        return {"checkbox": bool(value)}

    if pb_type == "date":
        if not value:
            return {"date": None}
        date_part = str(value).split(" ")[0].split("T")[0]
        return {"date": {"start": date_part}}

    if pb_type == "select":
        if max_select == 1:
            return {"select": {"name": str(value)} if value else None}
        items = value if isinstance(value, list) else ([value] if value else [])
        return {"multi_select": [{"name": str(v)} for v in items]}

    if pb_type == "relation":
        ids = value if isinstance(value, list) else ([value] if value else [])
        # Caller must map pb_id -> notion_id before calling this for relations.
        return {"relation": [{"id": i} for i in ids if i]}

    if pb_type == "json":
        import json as _json
        return {"rich_text": [{"type": "text",
                                "text": {"content": _json.dumps(value, ensure_ascii=False)}}]}

    return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}


def notion_property_to_pb_field(prop: dict, *,
                                pb_type: str,
                                max_select: int = 1) -> Any:
    """Convert a Notion property (API response shape) to a PB value."""
    ntype = prop.get("type")

    if ntype == "title":
        return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))

    if ntype == "rich_text":
        return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))

    if ntype == "number":
        return prop.get("number")

    if ntype == "checkbox":
        return bool(prop.get("checkbox"))

    if ntype == "email":
        return prop.get("email") or ""

    if ntype == "url":
        return prop.get("url") or ""

    if ntype == "date":
        d = prop.get("date")
        if not d:
            return ""
        return d.get("start", "")

    if ntype == "select":
        s = prop.get("select")
        return (s or {}).get("name", "")

    if ntype == "multi_select":
        return [item.get("name", "") for item in prop.get("multi_select", [])]

    if ntype == "relation":
        return [r.get("id", "") for r in prop.get("relation", [])]

    if ntype == "people":
        return [p.get("id", "") for p in prop.get("people", [])]

    if ntype == "files":
        return [f.get("name", "") for f in prop.get("files", [])]

    return prop
```

- [ ] **Step 4: Run — verify they pass**

```powershell
python -m pytest tests/notion_sync/test_codec.py -v
```
Expected: all 20 tests pass.

- [ ] **Step 5: Commit**

```powershell
git add notion_sync/codec.py tests/notion_sync/test_codec.py
git commit -m "PR1: notion_sync.codec — PB <-> Notion field conversion + tests"
```

---

## Task 7: `notion_sync/matching.py` — Fuzzy matcher (TDD)

**Files:**
- Create: `notion_sync/matching.py`
- Create: `tests/notion_sync/test_matching.py`

- [ ] **Step 1: Write failing tests**

`tests/notion_sync/test_matching.py`:
```python
"""Fuzzy match tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.matching import (
    normalize_title,
    bigram_jaccard,
    best_match,
)


def test_normalize_title_lowercases_and_strips():
    assert normalize_title("  Trip to Paris  ") == "trip to paris"
    assert normalize_title("Trip-to-Paris!") == "trip to paris"


def test_normalize_handles_chinese():
    assert normalize_title("巴黎旅行") == "巴黎旅行"


def test_bigram_jaccard_identical():
    assert bigram_jaccard("paris", "paris") == 1.0


def test_bigram_jaccard_similar():
    s = bigram_jaccard("trip to paris", "trip to parris")
    assert 0.7 < s < 1.0


def test_bigram_jaccard_unrelated():
    assert bigram_jaccard("paris", "tokyo") < 0.1


def test_best_match_exact():
    candidates = [
        {"id": "a", "title": "Trip to Paris", "date": "2026-06-15"},
        {"id": "b", "title": "Trip to Tokyo", "date": "2026-07-01"},
    ]
    target = {"title": "Trip to Paris", "date": "2026-06-15"}
    m = best_match(target, candidates, title_key="title", date_key="date")
    assert m.record["id"] == "a"
    assert m.score >= 0.95


def test_best_match_fuzzy_title_same_date():
    candidates = [
        {"id": "a", "title": "Trip to Paris!", "date": "2026-06-15"},
    ]
    target = {"title": "Trip to Paris", "date": "2026-06-15"}
    m = best_match(target, candidates, title_key="title", date_key="date")
    assert m.record["id"] == "a"
    assert m.score >= 0.85


def test_best_match_different_date_penalized():
    candidates = [
        {"id": "a", "title": "Trip to Paris", "date": "2026-06-15"},
        {"id": "b", "title": "Trip to Paris", "date": "2027-01-01"},
    ]
    target = {"title": "Trip to Paris", "date": "2026-06-15"}
    m = best_match(target, candidates, title_key="title", date_key="date")
    assert m.record["id"] == "a"


def test_best_match_no_candidates():
    m = best_match({"title": "X", "date": ""}, [], title_key="title", date_key="date")
    assert m is None


def test_best_match_below_threshold():
    candidates = [{"id": "a", "title": "Unrelated thing", "date": ""}]
    target = {"title": "Completely different", "date": ""}
    m = best_match(target, candidates, title_key="title", date_key="date",
                   min_score=0.6)
    assert m is None or m.score < 0.6
```

- [ ] **Step 2: Run — verify they fail**

```powershell
python -m pytest tests/notion_sync/test_matching.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`notion_sync/matching.py`:
```python
"""Fuzzy matching for the initial reconcile.

Strategy:
  1. Normalize titles (lowercase, strip non-alnum-and-CJK, collapse whitespace).
  2. Character-bigram Jaccard similarity.
  3. Date weighting: same date → +0.1; differing dates → ×0.5.
  4. Caller picks thresholds (auto-link >= 0.95, queue >= 0.60).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


@dataclass
class Match:
    record: dict
    score: float


def normalize_title(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    # Preserve word chars (Unicode \w covers Latin + CJK + Korean etc.).
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def bigram_jaccard(a: str, b: str) -> float:
    a, b = normalize_title(a), normalize_title(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def best_match(target: dict, candidates: list[dict], *,
               title_key: str,
               date_key: str = "",
               min_score: float = 0.0) -> Match | None:
    if not candidates:
        return None

    target_title = str(target.get(title_key) or "")
    target_date = str(target.get(date_key) or "") if date_key else ""

    best: Match | None = None
    for c in candidates:
        title_score = bigram_jaccard(target_title, str(c.get(title_key) or ""))
        score = title_score
        if date_key:
            c_date = str(c.get(date_key) or "")
            if target_date and c_date:
                if target_date == c_date:
                    score = min(1.0, score + 0.1)
                else:
                    score *= 0.5
        if best is None or score > best.score:
            best = Match(record=c, score=score)

    if best is None or best.score < min_score:
        return None
    return best
```

- [ ] **Step 4: Run — verify they pass**

```powershell
python -m pytest tests/notion_sync/test_matching.py -v
```
Expected: all 10 tests pass.

- [ ] **Step 5: Commit**

```powershell
git add notion_sync/matching.py tests/notion_sync/test_matching.py
git commit -m "PR1: notion_sync.matching — fuzzy title+date matcher + tests"
```

---

## Task 8: `notion_sync/backup.py` — PB JSON snapshot (TDD)

**Files:**
- Create: `notion_sync/backup.py`
- Create: `tests/notion_sync/test_backup.py`

- [ ] **Step 1: Write failing test**

`tests/notion_sync/test_backup.py`:
```python
"""Backup helper — uses a fake PB client."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.backup import backup_collections


class FakePB:
    def list_collections(self):
        return [{"name": "trips", "type": "base"},
                {"name": "todos", "type": "base"},
                {"name": "users", "type": "auth"}]   # skipped

    def list_records(self, collection, **kw):
        if collection == "trips":
            return [{"id": "t1", "title": "Paris"}, {"id": "t2", "title": "Tokyo"}]
        if collection == "todos":
            return [{"id": "td1", "title": "Buy milk"}]
        return []


def test_backup_writes_json_per_base_collection(tmp_path):
    pb = FakePB()
    out_dir = backup_collections(pb, root=tmp_path)

    assert out_dir.exists()
    assert (out_dir / "trips.json").exists()
    assert (out_dir / "todos.json").exists()
    assert not (out_dir / "users.json").exists()

    trips = json.loads((out_dir / "trips.json").read_text(encoding="utf-8"))
    assert len(trips) == 2
    assert trips[0]["title"] == "Paris"


def test_backup_creates_timestamped_subdir(tmp_path):
    out_dir = backup_collections(FakePB(), root=tmp_path)
    assert out_dir.parent == tmp_path
    assert len(out_dir.name) == 15
    assert out_dir.name[8] == "-"
```

- [ ] **Step 2: Run — verify it fails**

```powershell
python -m pytest tests/notion_sync/test_backup.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`notion_sync/backup.py`:
```python
"""Snapshot every PB base-collection to a timestamped folder.

Used before destructive operations (reconcile_initial, eventually PR3's
'Delete both' decisions). Notion has no equivalent because Notion's API
can't trigger a workspace backup — we accept that asymmetry by never doing
destructive Notion writes without a Sync Activity entry first.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def backup_collections(pb, root: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(root) / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    for c in pb.list_collections():
        if c.get("type") != "base":
            continue
        rows = pb.list_records(c["name"])
        path = out_dir / f"{c['name']}.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    return out_dir
```

- [ ] **Step 4: Run — verify it passes**

```powershell
python -m pytest tests/notion_sync/test_backup.py -v
```
Expected: both tests pass.

- [ ] **Step 5: Commit**

```powershell
git add notion_sync/backup.py tests/notion_sync/test_backup.py
git commit -m "PR1: notion_sync.backup — PB snapshot to .bridge_data/backups/"
```

---

## Task 9: `notion_sync/activity.py` — Sync Activity DB helpers

**Files:**
- Create: `notion_sync/activity.py`

Built on `NotionClient`. The Sync Activity DB id is read from env `NOTION_SYNC_ACTIVITY_DB_ID`, populated by `setup_notion_sync_db.py` in Task 10.

- [ ] **Step 1: Write the implementation**

```python
"""Helpers for writing rows to the Sync Activity Notion DB.

The DB itself is created once by scripts/setup_notion_sync_db.py and its
id is stored in env var NOTION_SYNC_ACTIVITY_DB_ID (also persisted to .env
on the VM by the bootstrap script).

Snapshots are JSON-stringified into rich_text so we can replay decisions
when the user picks Use Notion / Use PB / Delete both.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _rich(text: str) -> dict:
    if not text:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:200]}}]}


def _select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def _date(iso: str | None) -> dict:
    return {"date": {"start": iso} if iso else None}


def _url(href: str | None) -> dict:
    return {"url": href or None}


def write_auto_applied(client, *, collection: str, direction: str,
                       summary: str, pb_id: str, notion_id: str,
                       record_link: str | None = None) -> dict:
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    return client.create_page(db_id, {
        "title":        _title(f"{collection} · {direction} ({summary[:60]})"),
        "op":           _select("Auto-applied"),
        "direction":    _select(direction),
        "collection":   _select(collection),
        "record_link":  _url(record_link),
        "pb_id":        _rich(pb_id),
        "notion_id":    _rich(notion_id),
        "summary":      _rich(summary),
        "decision":     _select("N/A"),
        "detected_at":  _date(_now_iso()),
        "applied_at":   _date(_now_iso()),
    })


def write_conflict(client, *, collection: str, summary: str,
                   pb_id: str, notion_id: str,
                   pb_snapshot: dict, notion_snapshot: dict,
                   record_link: str | None = None) -> dict:
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    return client.create_page(db_id, {
        "title":           _title(f"{collection} · 冲突 ({summary[:60]})"),
        "op":              _select("Conflict"),
        "direction":       _select("None"),
        "collection":      _select(collection),
        "record_link":     _url(record_link),
        "pb_id":           _rich(pb_id),
        "notion_id":       _rich(notion_id),
        "summary":         _rich(summary),
        "pb_snapshot":     _rich(json.dumps(pb_snapshot, ensure_ascii=False)),
        "notion_snapshot": _rich(json.dumps(notion_snapshot, ensure_ascii=False)),
        "decision":        _select("Pending"),
        "detected_at":     _date(_now_iso()),
    })


def write_possible_duplicate(client, *, collection: str, summary: str,
                             pb_id: str, notion_id: str,
                             pb_snapshot: dict, notion_snapshot: dict,
                             score: float,
                             record_link: str | None = None) -> dict:
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    return client.create_page(db_id, {
        "title":           _title(f"{collection} · 可能重复 score={score:.2f}"),
        "op":              _select("Possible duplicate"),
        "direction":       _select("None"),
        "collection":      _select(collection),
        "record_link":     _url(record_link),
        "pb_id":           _rich(pb_id),
        "notion_id":       _rich(notion_id),
        "summary":         _rich(summary),
        "pb_snapshot":     _rich(json.dumps(pb_snapshot, ensure_ascii=False)),
        "notion_snapshot": _rich(json.dumps(notion_snapshot, ensure_ascii=False)),
        "decision":        _select("Pending"),
        "detected_at":     _date(_now_iso()),
    })


def write_delete_question(client, *, collection: str, summary: str,
                          pb_id: str, notion_id: str,
                          snapshot: dict) -> dict:
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    return client.create_page(db_id, {
        "title":           _title(f"{collection} · 删除? {summary[:60]}"),
        "op":              _select("Delete?"),
        "direction":       _select("None"),
        "collection":      _select(collection),
        "pb_id":           _rich(pb_id),
        "notion_id":       _rich(notion_id),
        "summary":         _rich(summary),
        "pb_snapshot":     _rich(json.dumps(snapshot, ensure_ascii=False)),
        "decision":        _select("Pending"),
        "detected_at":     _date(_now_iso()),
    })
```

- [ ] **Step 2: No unit test — exercised in reconcile integration. Commit.**

```powershell
git add notion_sync/activity.py
git commit -m "PR1: notion_sync.activity — Sync Activity DB write helpers"
```

---

## Task 10: `scripts/setup_notion_sync_db.py` — Bootstrap

**Files:**
- Create: `scripts/__init__.py` (if not present)
- Create: `scripts/setup_notion_sync_db.py`

Does:
1. For each of the 6 sync-target Notion DBs: add `pb_id` (rich_text) and `last_synced_at` (date) properties if not present.
2. Create the Sync Activity DB under a parent page (or reuse if `NOTION_SYNC_ACTIVITY_DB_ID` already set).
3. Seed `sync_config` rows in PB.

Idempotent — safe to re-run.

- [ ] **Step 1: Ensure `scripts/__init__.py` exists**

```powershell
ls scripts/__init__.py
# If "Path Not Found":
New-Item -ItemType File scripts/__init__.py | Out-Null
```

- [ ] **Step 2: Write the bootstrap script**

`scripts/setup_notion_sync_db.py`:
```python
#!/usr/bin/env python3
"""One-time bootstrap for Notion ↔ PB sync (PR1).

Does:
  1. Add pb_id + last_synced_at columns to each of the 6 sync-target
     Notion DBs (idempotent — checks for existing properties first).
  2. Create the Sync Activity Notion DB under a parent page.
  3. Seed sync_config rows in PB (one per sync target).

Run:
    python3 scripts/setup_notion_sync_db.py --parent-page-id <UUID>
    # or set NOTION_SYNC_PARENT_PAGE_ID in .env
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient


# Notion DB ID for each sync-target PB collection.
# Copied from pocketbase/migrate_notion.py's DBS list (only the 6 we sync).
SYNC_TARGETS: dict[str, str] = {
    "trips":     "df7ea062-7b18-4c4f-98f1-bfec8258c3db",
    "days":      "13329dea-4f55-4fc8-8e64-6c1ff19353bb",
    "plans":     "c951c7a9-a8f5-4ffd-aea2-1244e437ae46",
    "todos":     "5d4e3f93-cf13-4707-97c5-59b38940baac",
    "contacts":  "e304a6c3-4771-4c69-9ffc-97a672a1ac0c",
    "locations": "257c34c1-ac50-455d-9c8a-8d810de5c1e5",
}


SYNC_ACTIVITY_PROPERTIES = {
    "title":           {"title": {}},
    "op":              {"select": {"options": [
        {"name": "Auto-applied"}, {"name": "Conflict"},
        {"name": "Delete?"},     {"name": "Possible duplicate"},
        {"name": "Schema mismatch"},
    ]}},
    "direction":       {"select": {"options": [
        {"name": "Notion→PB"}, {"name": "PB→Notion"},
        {"name": "Both"},      {"name": "None"},
    ]}},
    "collection":      {"select": {"options": [
        {"name": c} for c in SYNC_TARGETS
    ]}},
    "record_link":     {"url": {}},
    "pb_id":           {"rich_text": {}},
    "notion_id":       {"rich_text": {}},
    "summary":         {"rich_text": {}},
    "pb_snapshot":     {"rich_text": {}},
    "notion_snapshot": {"rich_text": {}},
    "decision":        {"select": {"options": [
        {"name": "Pending"},     {"name": "Use Notion"},
        {"name": "Use PB"},      {"name": "Delete both"},
        {"name": "Keep both"},   {"name": "Merge"},
        {"name": "N/A"},
    ]}},
    "detected_at":     {"date": {}},
    "applied_at":      {"date": {}},
    "notes":           {"rich_text": {}},
}


def add_pipeline_columns(nc: NotionClient, db_id: str) -> None:
    db = nc.retrieve_database(db_id)
    existing = set(db.get("properties", {}).keys())
    patch: dict = {}
    if "pb_id" not in existing:
        patch["pb_id"] = {"rich_text": {}}
    if "last_synced_at" not in existing:
        patch["last_synced_at"] = {"date": {}}
    if not patch:
        print(f"  [skip] {db_id}: pipeline columns already present")
        return
    nc.update_database(db_id, {"properties": patch})
    print(f"  [ok]   {db_id}: added {list(patch.keys())}")


def find_or_create_activity_db(nc: NotionClient, parent_page_id: str) -> str:
    existing = os.environ.get("NOTION_SYNC_ACTIVITY_DB_ID")
    if existing:
        try:
            nc.retrieve_database(existing)
            print(f"  [skip] activity DB already configured: {existing}")
            return existing
        except RuntimeError:
            print(f"  [warn] NOTION_SYNC_ACTIVITY_DB_ID={existing} not found, creating new")

    db = nc.create_database(parent_page_id, "Sync Activity", SYNC_ACTIVITY_PROPERTIES)
    print(f"  [ok]   created Sync Activity DB: {db['id']}")
    print(f"         ADD TO .env: NOTION_SYNC_ACTIVITY_DB_ID={db['id']}")
    return db["id"]


def seed_sync_config(pb: PBClient) -> None:
    existing = {r["collection"]: r for r in pb.list_records("sync_config")}
    for name, notion_db_id in SYNC_TARGETS.items():
        payload = {
            "collection": name,
            "notion_db_id": notion_db_id,
            "enabled": True,
            "field_map_overrides": {},
        }
        if name in existing:
            pb.update_record("sync_config", existing[name]["id"], payload)
            print(f"  [upd] sync_config[{name}]")
        else:
            pb.create_record("sync_config", payload)
            print(f"  [new] sync_config[{name}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent-page-id",
                    default=os.environ.get("NOTION_SYNC_PARENT_PAGE_ID"),
                    help="Notion page under which Sync Activity DB is created")
    args = ap.parse_args()
    if not args.parent_page_id:
        print("error: pass --parent-page-id or set NOTION_SYNC_PARENT_PAGE_ID")
        return 1

    nc = NotionClient()
    pb = PBClient()

    print("[1/3] Adding pipeline columns to existing Notion DBs:")
    for db_id in SYNC_TARGETS.values():
        add_pipeline_columns(nc, db_id)

    print("[2/3] Setting up Sync Activity DB:")
    find_or_create_activity_db(nc, args.parent_page_id)

    print("[3/3] Seeding sync_config rows in PB:")
    seed_sync_config(pb)

    print("\nDone. Next: run `scripts/reconcile_initial.py --dry-run` to preview reconcile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run it on dashboard-server**

User picks a parent Notion page (e.g. a "Sync" sub-page under Smart Note), copies its page id from the URL (the 32-char hex after the page title — Notion shows it with dashes when you click "Copy link").

```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && .venv/bin/python scripts/setup_notion_sync_db.py --parent-page-id <PARENT_PAGE_ID>"
```
Expected output:
```
[1/3] Adding pipeline columns to existing Notion DBs:
  [ok]   df7ea062-...: added ['pb_id', 'last_synced_at']
  [ok]   13329dea-...: added ['pb_id', 'last_synced_at']
  ... (4 more)
[2/3] Setting up Sync Activity DB:
  [ok]   created Sync Activity DB: <UUID>
         ADD TO .env: NOTION_SYNC_ACTIVITY_DB_ID=<UUID>
[3/3] Seeding sync_config rows in PB:
  [new] sync_config[trips]
  [new] sync_config[days]
  ... (4 more)
```

User then appends the printed line to `/home/dev/phone-bridge/.env`:
```bash
ssh dashboard-server 'echo "NOTION_SYNC_ACTIVITY_DB_ID=<UUID>" >> /home/dev/phone-bridge/.env'
```

- [ ] **Step 4: Verify in Notion UI**

User opens Notion, navigates to the parent page → sees a new "Sync Activity" sub-DB.
Opens one of the 6 sync target DBs → confirms `pb_id` and `last_synced_at` columns are present. Right-click each → "Hide in view" to keep them out of default views.

- [ ] **Step 5: Commit**

```powershell
git add scripts/setup_notion_sync_db.py scripts/__init__.py
git commit -m "PR1: scripts/setup_notion_sync_db.py — Notion-side bootstrap"
```

---

## Task 11: `scripts/reconcile_initial.py` — One-shot data alignment

**Files:**
- Create: `scripts/reconcile_initial.py`

For each `sync_config` row with `enabled=true`:
1. Match by existing IDs (skip already-linked rows).
2. Fuzzy-match by title+date. Score ≥ 0.95 → auto-link; ≥ 0.60 → queue as `Possible duplicate`.
3. Residual unmatched: create matching pages on the opposite side.

Backs up PB to `.bridge_data/backups/<ts>/` before any write.

- [ ] **Step 1: Write the orchestrator**

`scripts/reconcile_initial.py`:
```python
#!/usr/bin/env python3
"""Initial Notion ↔ PB data alignment (PR1 one-shot).

For each enabled sync_config row:
  1. Skip already-linked rows.
  2. Fuzzy-match by title + date.
       score >= 0.95  → auto-link (write both IDs back)
       score >= 0.60  → write Possible duplicate to Sync Activity
       otherwise      → unmatched
  3. For unmatched residuals: create matching pages/records on the opposite
     side and write IDs back.

Backs up PB to .bridge_data/backups/<ts>/ before any write.

Run:
    python3 scripts/reconcile_initial.py            # full
    python3 scripts/reconcile_initial.py --only trips
    python3 scripts/reconcile_initial.py --dry-run  # log, write nothing
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.activity import write_possible_duplicate
from notion_sync.backup import backup_collections
from notion_sync.codec import (
    notion_property_to_pb_field,
    pb_field_to_notion_property,
    snake_to_title,
    title_to_snake,
)
from notion_sync.matching import best_match
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient


TITLE_FIELD_BY_COLLECTION = {
    "trips": "title", "plans": "title", "todos": "title",
    "days":  "name",  "contacts": "name", "locations": "name",
}
DATE_FIELD_BY_COLLECTION = {
    "trips": "date_start", "days": "date",
    "todos": "due_date",   "plans": "target_date",
    "contacts": "",        "locations": "",
}


def collection_field_types(pb: PBClient, name: str) -> dict[str, dict]:
    for c in pb.list_collections():
        if c["name"] == name:
            return {
                f["name"]: {"type": f["type"], "maxSelect": f.get("maxSelect", 1)}
                for f in c.get("fields", [])
            }
    raise RuntimeError(f"collection not found: {name}")


def now_iso_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_iso_datetime() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
                              title_field: str) -> dict:
    SKIP = {"id", "created", "updated", "collectionId", "collectionName",
            "expand", "notion_id", "notion_last_edited", "last_synced_at"}
    notion_title_prop = snake_to_title(title_field)
    props: dict = {}
    for pb_name, value in record.items():
        if pb_name in SKIP:
            continue
        if pb_name not in field_types:
            continue
        # Skip the title field — we promote it to a `title` property below.
        if pb_name == title_field:
            continue
        spec = field_types[pb_name]
        notion_name = overrides_inv.get(pb_name, snake_to_title(pb_name))
        props[notion_name] = pb_field_to_notion_property(
            value, pb_type=spec["type"], max_select=spec.get("maxSelect", 1)
        )
    title_val = record.get(title_field, "") or ""
    props[notion_title_prop] = {"title": [{"type": "text",
                                            "text": {"content": str(title_val)[:200]}}]}
    return props


def _pb_id_in_notion_page(p: dict) -> str:
    prop = p.get("properties", {}).get("pb_id", {})
    return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))


def reconcile_one(collection: str, notion_db_id: str,
                  overrides: dict[str, str],
                  pb: PBClient, nc: NotionClient,
                  dry_run: bool) -> dict:
    print(f"\n=== {collection} ===")
    overrides_inv = {v: k for k, v in overrides.items()}
    field_types = collection_field_types(pb, collection)
    title_field = TITLE_FIELD_BY_COLLECTION.get(collection, "title")
    date_field = DATE_FIELD_BY_COLLECTION.get(collection, "")

    pb_rows = pb.list_records(collection)
    notion_rows = nc.query_database(notion_db_id)
    print(f"  PB: {len(pb_rows)} rows  |  Notion: {len(notion_rows)} pages")

    # Phase 1: already linked (skip).
    pb_id_set = {r["id"] for r in pb_rows}
    linked = 0
    notion_unmatched: list[dict] = []
    for p in notion_rows:
        pid = _pb_id_in_notion_page(p)
        if pid and pid in pb_id_set:
            linked += 1
        else:
            notion_unmatched.append(p)
    pb_unmatched = [r for r in pb_rows if not r.get("notion_id")]
    print(f"  already linked: {linked}")

    # Phase 2: fuzzy match. Pre-flatten notion pages for matching.
    pb_candidates = [
        {"_pb": r,
         "title": r.get(title_field, "") or "",
         "date":  r.get(date_field, "") if date_field else ""}
        for r in pb_unmatched
    ]
    used_pb_ids: set[str] = set()
    used_notion_ids: set[str] = set()
    auto_linked = 0
    queued = 0

    for npage in notion_unmatched:
        npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
        target = {
            "title": npage_dict.get(title_field, "") or "",
            "date":  npage_dict.get(date_field, "") if date_field else "",
        }
        free = [c for c in pb_candidates if c["_pb"]["id"] not in used_pb_ids]
        m = best_match(target, free, title_key="title", date_key="date",
                        min_score=0.0)
        if m is None:
            continue
        if m.score >= 0.95:
            if dry_run:
                print(f"  [dry] auto-link Notion={npage['id'][:8]} ↔ "
                       f"PB={m.record['_pb']['id'][:8]} ({m.score:.2f})")
            else:
                pb.update_record(collection, m.record["_pb"]["id"], {
                    "notion_id": npage["id"],
                    "notion_last_edited": npage.get("last_edited_time"),
                    "last_synced_at": now_iso_datetime(),
                })
                nc.update_page(npage["id"], properties={
                    "pb_id": {"rich_text": [{"type": "text",
                                              "text": {"content": m.record["_pb"]["id"]}}]},
                    "last_synced_at": {"date": {"start": now_iso_date()}},
                })
            used_pb_ids.add(m.record["_pb"]["id"])
            used_notion_ids.add(npage["id"])
            auto_linked += 1
        elif m.score >= 0.60:
            if not dry_run:
                write_possible_duplicate(
                    nc,
                    collection=collection,
                    summary=f"{target['title'][:40]} ≈ {m.record['title'][:40]} "
                             f"(score {m.score:.2f})",
                    pb_id=m.record["_pb"]["id"],
                    notion_id=npage["id"],
                    pb_snapshot=m.record["_pb"],
                    notion_snapshot=npage_dict,
                    score=m.score,
                )
            else:
                print(f"  [dry] queue Notion={npage['id'][:8]} ↔ "
                       f"PB={m.record['_pb']['id'][:8]} ({m.score:.2f})")
            queued += 1

    # Phase 3a: PB-only rows → create Notion pages.
    pb_only = [r for r in pb_unmatched if r["id"] not in used_pb_ids]
    pb_only_created = 0
    for r in pb_only:
        props = pb_record_to_notion_props(r, field_types, overrides_inv, title_field)
        props["pb_id"] = {"rich_text": [{"type": "text", "text": {"content": r["id"]}}]}
        props["last_synced_at"] = {"date": {"start": now_iso_date()}}
        if dry_run:
            print(f"  [dry] create Notion for PB={r['id'][:8]} "
                   f"title={r.get(title_field, '')[:30]!r}")
        else:
            page = nc.create_page(notion_db_id, props)
            pb.update_record(collection, r["id"], {
                "notion_id": page["id"],
                "notion_last_edited": page.get("last_edited_time"),
                "last_synced_at": now_iso_datetime(),
            })
        pb_only_created += 1

    # Phase 3b: Notion-only pages → create PB records.
    notion_only = [p for p in notion_unmatched if p["id"] not in used_notion_ids]
    notion_only_created = 0
    for npage in notion_only:
        npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
        if dry_run:
            print(f"  [dry] create PB for Notion={npage['id'][:8]} "
                   f"title={npage_dict.get(title_field, '')[:30]!r}")
        else:
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
        notion_only_created += 1

    summary = (f"linked={linked} auto-linked={auto_linked} "
                f"queued={queued} pb→notion={pb_only_created} "
                f"notion→pb={notion_only_created}")
    print(f"  summary: {summary}")
    return {"summary": summary}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="single collection (e.g. trips)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pb = PBClient()
    nc = NotionClient()

    if not os.environ.get("NOTION_SYNC_ACTIVITY_DB_ID"):
        print("error: NOTION_SYNC_ACTIVITY_DB_ID not set — run "
              "scripts/setup_notion_sync_db.py first")
        return 1

    if not args.dry_run:
        backup_root = Path(os.environ.get("BRIDGE_DATA_DIR", ".bridge_data")) / "backups"
        out = backup_collections(pb, backup_root)
        print(f"PB backup written: {out}")

    targets = pb.list_records("sync_config", filter="enabled=true")
    if args.only:
        targets = [t for t in targets if t["collection"] == args.only]
        if not targets:
            print(f"error: no enabled sync_config row for collection={args.only!r}")
            return 1

    for t in targets:
        try:
            result = reconcile_one(
                collection=t["collection"],
                notion_db_id=t["notion_db_id"],
                overrides=t.get("field_map_overrides") or {},
                pb=pb, nc=nc,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                pb.update_record("sync_config", t["id"], {
                    "last_synced_at": now_iso_datetime(),
                    "last_sync_summary": "reconcile_initial: " + result["summary"],
                })
        except Exception as e:
            print(f"  !! reconcile FAILED for {t['collection']}: {e}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Dry-run on the smallest collection**

```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && .venv/bin/python scripts/reconcile_initial.py --dry-run --only contacts"
```

Expected: lines like `[dry] auto-link Notion=abc12345 ↔ PB=rec67890 (1.00)`, no errors, no writes. If field names don't match (e.g. PB has `last_contact` but Notion has `Last Contact` and codec turns it into `last_contact` correctly — verify the dry-run doesn't report `skip: field not on PB side` for things that should map).

If there are unmatched fields, add overrides to that collection's `sync_config.field_map_overrides`:
```bash
# Example: Notion has "Last contact" (lowercase c) → maps to "last_contact"
# title_to_snake("Last contact") == "last_contact" — already correct, no override needed.
# But Notion property "Executor Ref ID" → "executor_ref_id" — also correct.
# Override only when title_to_snake fails (e.g. Notion uses Chinese: "出发时间").
```

- [ ] **Step 3: Real run, one collection at a time**

```powershell
# Start with the smallest, lowest-risk collection.
ssh dashboard-server "cd /home/dev/phone-bridge && .venv/bin/python scripts/reconcile_initial.py --only locations"
```

Verify:
- PB backup folder exists: `ssh dashboard-server 'ls -la /home/dev/phone-bridge/.bridge_data/backups/ | tail -3'`
- Spot-check 3 rows in Notion `locations` DB → `pb_id` populated
- Spot-check 3 rows in PB locations admin → `notion_id` populated
- Open Notion Sync Activity → `Possible duplicate` count matches dry-run output

Iterate through `contacts`, `todos`, `plans`, `days`, `trips`. Inspect after each.

- [ ] **Step 4: Resolve any `Possible duplicate` items**

For each Sync Activity row with `op=Possible duplicate`:
- Open the row
- Look at `pb_snapshot` and `notion_snapshot`
- Set `decision` to `Use Notion` / `Use PB` / `Keep both` (whichever fits)

PR3 will auto-apply these. For PR1 the choice is just recorded for later.

- [ ] **Step 5: Commit**

```powershell
git add scripts/reconcile_initial.py
git commit -m "PR1: scripts/reconcile_initial.py — one-shot data alignment"
```

---

## Task 12: Update `CLAUDE.md` with sync workflow

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a section after "When NOT to deploy" and before "Architecture"**

Insert this block into `CLAUDE.md`:

```markdown
## Notion sync (PR1 baseline)

PR1 wires up the schema and the initial data alignment between PB and Notion
for 6 collections (trips/days/plans/todos/contacts/locations). Cron-driven
sync lands in PR2; this section covers manual operations only.

**Sync metadata:**
- PB collections `sync_config` (per-collection) and `sync_global` (timezone,
  sync hour, paused) — created by migration `1779465616_create_sync_meta.js`
- Pipeline fields on each synced PB collection: `notion_id`,
  `notion_last_edited`, `last_synced_at` — added by migration
  `1779465617_add_sync_pipeline_fields.js`
- Pipeline columns on each Notion DB: `pb_id`, `last_synced_at` — added by
  `scripts/setup_notion_sync_db.py`
- Notion DB "Sync Activity" — created by `setup_notion_sync_db.py`, id stored
  in `.env` as `NOTION_SYNC_ACTIVITY_DB_ID`

**Re-running reconcile:**

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
.venv/bin/python scripts/reconcile_initial.py --only <collection> --dry-run
.venv/bin/python scripts/reconcile_initial.py --only <collection>
```

The script backs up PB to `.bridge_data/backups/<ts>/` before any write, so
partial failures are recoverable.

**Reviewing duplicates:** open the Sync Activity Notion DB → filter for
`op=Possible duplicate` and `decision=Pending` → for each row, look at the
two snapshots and set `decision` to `Use Notion` / `Use PB` / `Keep both`.
PR3 will auto-apply these; for PR1 the choice is just recorded.
```

- [ ] **Step 2: Commit**

```powershell
git add CLAUDE.md
git commit -m "PR1: document Notion sync workflow in CLAUDE.md"
```

---

## Self-Review Checklist (run after all tasks complete)

- [ ] All 6 sync target PB collections have `notion_id`, `notion_last_edited`, `last_synced_at`. Spot check 3.
- [ ] Every PB row in the 6 sync targets has a non-empty `notion_id` (or its match was queued as `Possible duplicate`). Quick check:
  ```bash
  ssh dashboard-server '
    TOKEN=$(curl -s -X POST http://127.0.0.1:8090/api/collections/_superusers/auth-with-password -H "Content-Type: application/json" -d "{\"identity\":\"$POCKETBASE_ADMIN_EMAIL\",\"password\":\"$POCKETBASE_ADMIN_PASSWORD\"}" | jq -r .token);
    for c in trips days plans todos contacts locations; do
      f=$(printf "notion_id=''" | python3 -c "import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read()))");
      n=$(curl -s -H "Authorization: $TOKEN" "http://127.0.0.1:8090/api/collections/$c/records?filter=$f&perPage=1" | jq .totalItems);
      echo "$c: $n unlinked";
    done'
  ```
  Expected: each `0` or matches the queued-duplicate count.
- [ ] Sync Activity Notion DB exists with all 13 properties.
- [ ] `sync_config` has 6 rows, all `enabled=true`.
- [ ] `sync_global` has 1 row: `timezone=America/New_York`, `sync_hour_local=3`, `paused=false`.
- [ ] PB backup folder `.bridge_data/backups/<ts>/` exists with one JSON per base collection.
- [ ] `python -m pytest tests/notion_sync/ -v` — all green.

---

## Out of scope (deferred to PR2/PR3)

- systemd timer + cron-driven incremental sync (PR2)
- `op=Conflict` and `op=Delete?` detection (PR2 logs only, PR3 enqueues)
- Sync Activity decision applier (PR3)
- Push notification on Pending count (PR3)
- MCP tools `sync_now`, `sync_queue_status`, `sync_pause` (PR3)
- Schema-drift detection (deferred indefinitely — manual)
- Relation-field sync correctness (assumed identical schemas; revisit in PR2)
- 30-/90-day auto-cleanup of resolved Sync Activity rows (PR3)
