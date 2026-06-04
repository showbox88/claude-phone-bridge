"""In-process PocketBase tools for the phone-bridge Claude SDK session.

Mirrors the CRUD surface of `mcp_pb/server.py` (the claude.ai Custom Connector)
but runs *inside* phone-bridge so the on-device Claude Code/Chat session can
read and write Smart Note data directly through `mcp__pb__*` tools instead of
hand-rolling Bash + curl. Talks to the local PocketBase over HTTP; superuser
credentials come from the same POCKETBASE_* env vars that `server.py` reads.

Auth is self-contained here (own 25-min token cache + re-auth on miss) so the
tools keep working regardless of `server.py`'s 12-h PB_TOKEN refresh loop.

Tool surface (matches mcp_pb):
  read / safe-write  -> pre-approved in server.py (no phone prompt)
    pb_list_collections, pb_search, pb_get, pb_get_collection,
    pb_create, pb_update, smartnote_open_context
  destructive        -> left out of allowed_tools, so they hit the
    pb_delete, pb_create_collection,        permission prompt like any
    pb_update_collection, pb_delete_collection   other risky tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from notion_sync.config import collections_with_auto_sync

log = logging.getLogger("bridge.pb")

SERVER_NAME = "pb"

PB_URL      = os.environ.get("POCKETBASE_URL", "http://127.0.0.1:8090").rstrip("/")
PB_EMAIL    = os.environ.get("POCKETBASE_ADMIN_EMAIL", "")

# ---------------------------------------------------------------------------
# Auto-sync: after pb_create / pb_update / pb_delete on a sync-target
# collection, schedule a delayed runner --force-now --only X. Multiple
# writes within the debounce window collapse into one runner pass per
# touched collection. Lets check-ins (and any other write surface) feel
# instantaneous in Notion without flooding the runner.
# ---------------------------------------------------------------------------
_AUTO_SYNC_DEBOUNCE_SECS = 10.0
_pending_sync: set[str] = set()
_sync_task: asyncio.Task | None = None


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


async def _run_debounced_sync() -> None:
    """After the debounce window, snapshot pending collections and run
    `python -m notion_sync.runner --force-now --only X` for each. Errors
    are logged but never propagate."""
    try:
        await asyncio.sleep(_AUTO_SYNC_DEBOUNCE_SECS)
    except asyncio.CancelledError:
        return
    cols = sorted(_pending_sync)
    _pending_sync.clear()
    bridge_root = os.path.dirname(os.path.abspath(__file__))
    for col in cols:
        try:
            proc = await asyncio.create_subprocess_exec(
                "/home/dev/phone-bridge/.venv/bin/python", "-m", "notion_sync.runner",
                "--force-now", "--only", col,
                cwd=bridge_root,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=300)
        except Exception as e:
            log.warning("auto-sync runner failed for %s: %s", col, e)
PB_PASSWORD = os.environ.get("POCKETBASE_ADMIN_PASSWORD", "")


def enabled() -> bool:
    """True only when PocketBase superuser creds are configured."""
    return bool(PB_URL and PB_EMAIL and PB_PASSWORD)


# ---------------------------------------------------------------------------
# PocketBase HTTP helpers (blocking urllib; tools wrap these in to_thread so
# the FastAPI event loop never blocks on PB I/O).
# ---------------------------------------------------------------------------
def _http(method: str, url: str, body: Any | None = None, headers: dict | None = None,
          timeout: float = 15.0) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:500]}


_pb_token: str | None = None
_pb_token_expiry: float = 0.0


def _pb_auth() -> str:
    global _pb_token, _pb_token_expiry
    if _pb_token and time.time() < _pb_token_expiry:
        return _pb_token
    code, data = _http("POST", f"{PB_URL}/api/collections/_superusers/auth-with-password",
                       body={"identity": PB_EMAIL, "password": PB_PASSWORD},
                       headers={"Content-Type": "application/json"})
    if code != 200:
        raise RuntimeError(f"PB auth failed: {code} {data}")
    _pb_token = data["token"]
    _pb_token_expiry = time.time() + 25 * 60
    return _pb_token


def _pb_sync(method: str, path: str, body: Any | None = None) -> Any:
    code, data = _http(method, f"{PB_URL}{path}", body=body, headers={
        "Authorization": _pb_auth(),
        "Content-Type": "application/json",
    })
    if code >= 400:
        raise RuntimeError(f"PB {method} {path}: {code} {data}")
    return data


async def _pb(method: str, path: str, body: Any | None = None) -> Any:
    return await asyncio.to_thread(_pb_sync, method, path, body)


def _ok(data: Any) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(data, ensure_ascii=False, indent=2)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@tool(
    "pb_list_collections",
    "List all PocketBase collections with their fields and (for select fields) "
    "valid values. Call this at the start of a Smart Note conversation so you "
    "know the current schema and pick the right collection / select option.",
    {},
)
async def pb_list_collections(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    try:
        data = await _pb("GET", "/api/collections?perPage=100")
        out = []
        for c in data.get("items", []):
            if c.get("type") != "base":
                continue
            fields = []
            for f in c.get("fields", []):
                fdesc: dict[str, Any] = {"name": f["name"], "type": f["type"]}
                if f["type"] == "select":
                    fdesc["values"]    = f.get("values", [])
                    fdesc["maxSelect"] = f.get("maxSelect", 1)
                if f["type"] == "relation":
                    fdesc["target"]    = f.get("collectionId")
                    fdesc["maxSelect"] = f.get("maxSelect", 1)
                if f.get("required"):
                    fdesc["required"] = True
                fields.append(fdesc)
            out.append({"name": c["name"], "id": c["id"], "fields": fields})
        return _ok({"collections": out})
    except Exception as e:
        return _err(f"pb_list_collections failed: {e}")


@tool(
    "pb_search",
    "Search records in a PocketBase collection.\n\n"
    "Filter uses PB DSL: (field='value' && other!=0). Examples:\n"
    "  - status='Active' && priority='High'\n"
    "  - title~'idea'           (~ = LIKE)\n"
    "  - date >= '2026-01-01'\n"
    "Sort: comma list with '-' prefix for desc, e.g. '-date,title'.\n"
    "Expand: comma list of relation field names whose target records to embed.",
    {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "description": "Collection name"},
            "filter":     {"type": "string", "description": "PB filter DSL (optional)"},
            "sort":       {"type": "string", "description": "Sort spec, default '-created'"},
            "expand":     {"type": "string", "description": "Relation fields to embed (optional)"},
            "page":       {"type": "integer", "description": "1-based page, default 1"},
            "per_page":   {"type": "integer", "description": "Page size 1-200, default 30"},
        },
        "required": ["collection"],
    },
)
async def pb_search(args: dict[str, Any]) -> dict[str, Any]:
    try:
        collection = args["collection"]
        filter_    = args.get("filter", "")
        sort       = args.get("sort", "-created")
        expand     = args.get("expand", "")
        page       = int(args.get("page", 1))
        per_page   = int(args.get("per_page", 30))
        params = []
        if filter_:
            params.append("filter=" + urllib.parse.quote(filter_, safe=""))
        if sort:
            params.append("sort=" + urllib.parse.quote(sort, safe=",-"))
        if expand:
            params.append("expand=" + urllib.parse.quote(expand, safe=","))
        params.append(f"page={page}")
        params.append(f"perPage={min(max(per_page, 1), 200)}")
        data = await _pb("GET", f"/api/collections/{collection}/records?" + "&".join(params))
        return _ok(data)
    except Exception as e:
        return _err(f"pb_search failed: {e}")


@tool(
    "pb_get",
    "Get a single PocketBase record by ID, optionally with 'expand' for relations.",
    {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "id":         {"type": "string"},
            "expand":     {"type": "string", "description": "Relation fields to embed (optional)"},
        },
        "required": ["collection", "id"],
    },
)
async def pb_get(args: dict[str, Any]) -> dict[str, Any]:
    try:
        expand = args.get("expand", "")
        q = "?expand=" + urllib.parse.quote(expand, safe=",") if expand else ""
        data = await _pb("GET", f"/api/collections/{args['collection']}/records/{args['id']}{q}")
        return _ok(data)
    except Exception as e:
        return _err(f"pb_get failed: {e}")


@tool(
    "pb_create",
    "Create a record in a PocketBase collection. 'data' is a field map.\n\n"
    "PB auto-fills id, created, updated. For select fields use the exact string "
    "value (case-sensitive). For relation fields use the target record's id "
    "(single) or list of ids (multi).",
    {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "data":       {"type": "object", "description": "Field map for the new record"},
        },
        "required": ["collection", "data"],
    },
)
async def pb_create(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb("POST", f"/api/collections/{args['collection']}/records",
                         body=args["data"])
        _schedule_auto_sync(args["collection"])
        return _ok(data)
    except Exception as e:
        return _err(f"pb_create failed: {e}")


@tool(
    "pb_update",
    "Update specific fields of a PocketBase record. Pass only fields to change.\n\n"
    "Common patterns:\n"
    "  - Archive: pb_update(coll, id, {\"status\": \"Archived\"})\n"
    "  - Mark todo done: pb_update(\"todos\", id, {\"status\": \"Done\", "
    "\"completed_at\": \"2026-05-27\"})",
    {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "id":         {"type": "string"},
            "data":       {"type": "object", "description": "Fields to change"},
        },
        "required": ["collection", "id", "data"],
    },
)
async def pb_update(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb("PATCH",
                         f"/api/collections/{args['collection']}/records/{args['id']}",
                         body=args["data"])
        _schedule_auto_sync(args["collection"])
        return _ok(data)
    except Exception as e:
        return _err(f"pb_update failed: {e}")


@tool(
    "pb_delete",
    "Permanently delete a PocketBase record. Irreversible. Per Smart Note rules, "
    "prefer pb_update(coll, id, {\"status\": \"Archived\"}) for normal mistakes. "
    "Use real delete only when the user explicitly asks (\"hard delete\", "
    "\"really remove\", \"彻底删掉\"), or for obvious garbage like duplicate rows / "
    "test scaffolding / records the user never saw.",
    {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "id":         {"type": "string"},
        },
        "required": ["collection", "id"],
    },
)
async def pb_delete(args: dict[str, Any]) -> dict[str, Any]:
    try:
        await _pb("DELETE", f"/api/collections/{args['collection']}/records/{args['id']}")
        _schedule_auto_sync(args["collection"])
        return _ok({"ok": True, "collection": args["collection"], "deleted": args["id"]})
    except Exception as e:
        return _err(f"pb_delete failed: {e}")


@tool(
    "pb_get_collection",
    "Fetch the full definition of one collection (all fields with their raw "
    "config). Use before pb_update_collection to read the current field array, "
    "then mutate and patch it back.",
    {
        "type": "object",
        "properties": {"id_or_name": {"type": "string"}},
        "required": ["id_or_name"],
    },
)
async def pb_get_collection(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb("GET", f"/api/collections/{args['id_or_name']}")
        return _ok(data)
    except Exception as e:
        return _err(f"pb_get_collection failed: {e}")


@tool(
    "pb_create_collection",
    "Create a new PocketBase collection (table). 'fields' is a list of field-spec "
    "dicts; each needs at minimum 'name' and 'type'. Common types: text, editor "
    "(markdown), number, bool, date, email, url, select "
    "({\"type\":\"select\",\"maxSelect\":1,\"values\":[...]}), relation "
    "({\"type\":\"relation\",\"collectionId\":\"<id>\",\"maxSelect\":1}), json, "
    "file. PB auto-adds id/created/updated. Returns the created collection.",
    {
        "type": "object",
        "properties": {
            "name":   {"type": "string"},
            "fields": {"type": "array", "items": {"type": "object"},
                       "description": "List of field-spec dicts"},
            "type":   {"type": "string", "description": "Collection type, default 'base'"},
        },
        "required": ["name", "fields"],
    },
)
async def pb_create_collection(args: dict[str, Any]) -> dict[str, Any]:
    try:
        body = {"name": args["name"], "type": args.get("type", "base"),
                "fields": args["fields"]}
        data = await _pb("POST", "/api/collections", body=body)
        return _ok(data)
    except Exception as e:
        return _err(f"pb_create_collection failed: {e}")


@tool(
    "pb_update_collection",
    "Patch an existing collection (rename, add/remove/modify fields, indexes, "
    "rules). 'patch' is merged onto the current definition. To add a field, "
    "include the FULL fields array (existing + new) — read it first with "
    "pb_get_collection. Existing fields keep their data; new fields default to "
    "null for old rows.",
    {
        "type": "object",
        "properties": {
            "id_or_name": {"type": "string"},
            "patch":      {"type": "object", "description": "Fields to merge onto the collection"},
        },
        "required": ["id_or_name", "patch"],
    },
)
async def pb_update_collection(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb("PATCH", f"/api/collections/{args['id_or_name']}",
                         body=args["patch"])
        return _ok(data)
    except Exception as e:
        return _err(f"pb_update_collection failed: {e}")


@tool(
    "pb_delete_collection",
    "Delete a collection AND all its records. Irreversible. Use only when "
    "explicitly asked by the user.",
    {
        "type": "object",
        "properties": {"id_or_name": {"type": "string"}},
        "required": ["id_or_name"],
    },
)
async def pb_delete_collection(args: dict[str, Any]) -> dict[str, Any]:
    try:
        await _pb("DELETE", f"/api/collections/{args['id_or_name']}")
        return _ok({"ok": True, "deleted": args["id_or_name"]})
    except Exception as e:
        return _err(f"pb_delete_collection failed: {e}")


@tool(
    "smartnote_open_context",
    "Fetch active high-priority memos from claude_memos. Call at the start of a "
    "Smart Note conversation to recover persistent context.",
    {},
)
async def smartnote_open_context(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    try:
        f = urllib.parse.quote("status='Active' && priority='High'", safe="")
        data = await _pb("GET",
            f"/api/collections/claude_memos/records?filter={f}&sort=-date&perPage=50")
        return _ok(data)
    except Exception as e:
        return _err(f"smartnote_open_context failed: {e}")


# ---------------------------------------------------------------------------
# Notion sync tools — give Claude visibility + manual control over the
# daily sync runner without dropping to Bash.
# ---------------------------------------------------------------------------
@tool(
    "sync_now",
    "Run the Notion ↔ PocketBase sync runner immediately (bypasses the time "
    "guard). Optional 'collection' arg restricts to one sync target. Returns "
    "the summary: applied / conflicts / deletes / frozen_skipped / "
    "decisions_applied / pending counts. Use this when the user says 'sync "
    "now' or wants to push a fresh edit through.",
    {
        "type": "object",
        "properties": {
            "collection": {"type": "string",
                           "description": "Restrict to one of trips/days/stops/plans/todos/contacts/locations/journal"},
        },
    },
)
async def sync_now(args: dict[str, Any]) -> dict[str, Any]:
    import subprocess
    cmd = [
        "/home/dev/phone-bridge/.venv/bin/python", "-m", "notion_sync.runner",
        "--force-now",
    ]
    coll = args.get("collection")
    if coll:
        cmd += ["--only", coll]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            cwd="/home/dev/phone-bridge",
            capture_output=True, text=True, timeout=600,
        )
        log_path = "/home/dev/phone-bridge/.bridge_data/sync.log"
        tail: list[Any] = []
        try:
            with open(log_path, encoding="utf-8") as f:
                for line in f.readlines()[-12:]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        tail.append(json.loads(line))
                    except json.JSONDecodeError:
                        tail.append({"raw": line})
        except OSError:
            pass
        return _ok({
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
            "log_tail": tail,
        })
    except subprocess.TimeoutExpired:
        return _err("sync_now timed out after 10 minutes")
    except Exception as e:
        return _err(f"sync_now failed: {e}")


@tool(
    "sync_queue_status",
    "Read the Sync Activity Notion DB and report Pending counts + first "
    "few summaries. Use this to answer 'do I have any pending sync items?' "
    "without making the user open Notion. Does NOT modify anything.",
    {},
)
async def sync_queue_status(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    notion_token = os.environ.get("NOTION_TOKEN", "")
    db_id        = os.environ.get("NOTION_SYNC_ACTIVITY_DB_ID", "")
    if not notion_token or not db_id:
        return _err("NOTION_TOKEN or NOTION_SYNC_ACTIVITY_DB_ID not set")
    body = json.dumps({"filter": {"and": [
        {"property": "decision",   "select": {"equals": "Pending"}},
        {"property": "applied_at", "date":   {"is_empty": True}},
    ]}}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        data=body, method="POST",
        headers={
            "Authorization":  f"Bearer {notion_token}",
            "Notion-Version": "2022-06-28",
            "Content-Type":   "application/json",
        },
    )
    try:
        def _do() -> Any:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        data = await asyncio.to_thread(_do)
    except Exception as e:
        return _err(f"sync_queue_status query failed: {e}")
    rows = data.get("results", [])
    items: list[dict[str, Any]] = []
    for r in rows[:10]:
        p = r.get("properties", {})
        op   = (p.get("op", {}).get("select") or {}).get("name")
        coll = (p.get("collection", {}).get("select") or {}).get("name")
        summ = "".join(rt.get("plain_text", "")
                       for rt in p.get("summary", {}).get("rich_text", []))
        link = (p.get("record_link", {}) or {}).get("url")
        items.append({
            "op": op, "collection": coll, "summary": summ, "record_link": link,
            "sync_activity_page": r.get("url"),
        })
    return _ok({"pending": len(rows), "items": items})


@tool(
    "sync_pause",
    "Pause the daily Notion ↔ PB sync runner. While paused, the hourly "
    "cron exits silently and no sync happens. Use when the user wants "
    "to make a series of edits without the runner interfering.",
    {},
)
async def sync_pause(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    try:
        data = await _pb("GET", "/api/collections/sync_global/records?perPage=1")
        items = data.get("items", [])
        if not items:
            return _err("sync_global has no rows")
        row = items[0]
        await _pb("PATCH", f"/api/collections/sync_global/records/{row['id']}",
                  body={"paused": True})
        return _ok({"ok": True, "paused": True})
    except Exception as e:
        return _err(f"sync_pause failed: {e}")


@tool(
    "sync_resume",
    "Resume the daily Notion ↔ PB sync runner after a pause. The next "
    "hourly cron tick will run normally (only at the configured local hour).",
    {},
)
async def sync_resume(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    try:
        data = await _pb("GET", "/api/collections/sync_global/records?perPage=1")
        items = data.get("items", [])
        if not items:
            return _err("sync_global has no rows")
        row = items[0]
        await _pb("PATCH", f"/api/collections/sync_global/records/{row['id']}",
                  body={"paused": False})
        return _ok({"ok": True, "paused": False})
    except Exception as e:
        return _err(f"sync_resume failed: {e}")


# ---------------------------------------------------------------------------
# Server assembly + tool-name exports
# ---------------------------------------------------------------------------
_SAFE = [
    pb_list_collections, pb_search, pb_get, pb_get_collection,
    pb_create, pb_update, smartnote_open_context,
    sync_now, sync_queue_status, sync_pause, sync_resume,
]
_GATED = [
    pb_delete, pb_create_collection, pb_update_collection, pb_delete_collection,
]


def _qualified(name: str) -> str:
    return f"mcp__{SERVER_NAME}__{name}"


# Read + safe-write tools that server.py pre-approves (no phone permission prompt,
# matching the old "auto-allow localhost:8090 curl" fast-path). Names must match
# the strings passed to @tool above.
SAFE_TOOL_NAMES: list[str] = [_qualified(n) for n in (
    "pb_list_collections", "pb_search", "pb_get", "pb_get_collection",
    "pb_create", "pb_update", "smartnote_open_context",
    "sync_now", "sync_queue_status", "sync_pause", "sync_resume",
)]
# Destructive / schema-mutating tools — deliberately NOT pre-approved.
GATED_TOOL_NAMES: list[str] = [_qualified(n) for n in (
    "pb_delete", "pb_create_collection", "pb_update_collection",
    "pb_delete_collection",
)]


def build_server():
    """Create the in-process SDK MCP server holding every pb_* tool."""
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=_SAFE + _GATED,
    )


# A short prompt fragment server.py appends so Claude knows the tools exist.
PROMPT_HINT = (
    "You have direct PocketBase tools for the local Smart Note store "
    "(行程/地点/消费/美食/日记/待办/联系人/灵感/计划/交易/claude_memos/简报). "
    "Prefer them over Bash+curl for reading/writing PocketBase: "
    "mcp__pb__pb_list_collections (dump the schema first), pb_search, pb_get, "
    "pb_get_collection, pb_create, pb_update, and smartnote_open_context. "
    "Destructive ops (pb_delete, pb_create_collection, pb_update_collection, "
    "pb_delete_collection) require user approval, so only reach for them when "
    "asked. Call pb_list_collections at the start so you use the right "
    "collection names and exact select values.\n\n"
    "For the Notion ↔ PB sync (trips/days/stops/plans/todos/contacts/locations/journal): "
    "sync_queue_status shows Pending items the user needs to decide on; "
    "sync_now triggers an immediate sync (use when user says '同步一下'); "
    "sync_pause / sync_resume toggle sync_global.paused. Daily sync still "
    "fires automatically at the configured local hour — these tools are "
    "for on-demand control.\n\n"
    "AUTO-SYNC: pb_create / pb_update / pb_delete on collections marked "
    "auto_sync in sync_config (currently trips / days / stops / locations / "
    "todos / journal — visible in the 同步设置 page) automatically schedules "
    "a debounced sync "
    "(10s window — multiple quick writes batch into one runner pass per "
    "touched collection). You don't need to call sync_now manually after "
    "those writes. Auto-sync does NOT trigger for plans / contacts / foods / "
    "claude_memos / other PB-only collections — those wait for the next "
    "scheduled cron (default 03:00 + 15:00 local)."
)
