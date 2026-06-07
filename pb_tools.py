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
import sys
import time
import urllib.request
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from notion_sync.config import collections_with_auto_sync

from app.paths import BRIDGE_ROOT, SYNC_LOG
from app.settings import settings

log = logging.getLogger("bridge.pb")

SERVER_NAME = "pb"

PB_URL      = settings.pocketbase_url or "http://127.0.0.1:8090"
PB_EMAIL    = settings.pocketbase_admin_email

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

# Throttle the "registry unavailable" warning so a sustained PB outage
# doesn't flood the journal with one line per chat tool call. We log
# at most once per 60s while the failure persists.
_REGISTRY_WARN_INTERVAL_SECS = 60.0
_last_registry_warn_ts: float = 0.0


def _schedule_auto_sync(collection: str) -> None:
    """Add a collection to the pending set and (re-)arm the debounced runner.

    Whether a collection auto-syncs is now driven by sync_config rows
    (auto_sync=true + enabled=true). The set is cached for 60s by the
    loader so this is not a per-write PB hit in steady state.
    """
    try:
        auto = collections_with_auto_sync()
    except Exception as e:
        global _last_registry_warn_ts
        now_mono = time.monotonic()
        if now_mono - _last_registry_warn_ts >= _REGISTRY_WARN_INTERVAL_SECS:
            log.warning("auto-sync registry unavailable: %s", e)
            _last_registry_warn_ts = now_mono
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
    for col in cols:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "notion_sync.runner",
                "--force-now", "--only", col,
                cwd=str(BRIDGE_ROOT),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=300)
        except Exception as e:
            log.warning("auto-sync runner failed for %s: %s", col, e)
PB_PASSWORD = settings.pocketbase_admin_password


def enabled() -> bool:
    """True only when PocketBase superuser creds are configured."""
    return bool(PB_URL and PB_EMAIL and PB_PASSWORD)


# ---------------------------------------------------------------------------
# PocketBase HTTP client
# ---------------------------------------------------------------------------
# Phase 1: replaced bespoke _http / _pb_auth / _pb_sync / _pb (~50 lines)
# with the unified app.integrations.pb.AsyncPBClient. The client carries
# its own per-instance token cache + 5xx/429/401 retry logic.
#
# Separate process from server.py's _pb_client; that's fine — both
# instances authenticate independently against the same PB superuser
# credentials. PB issues independent admin tokens; no contention.

from app.agent.mcp_tools.prompts import TOOL_DESCRIPTIONS, TOOL_SCHEMAS
from app.integrations.pb import AsyncPBClient, PBError

_pb_client: AsyncPBClient | None = None


def _pb() -> AsyncPBClient:
    global _pb_client
    if _pb_client is None:
        _pb_client = AsyncPBClient(PB_URL, PB_EMAIL, PB_PASSWORD)
    return _pb_client


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
    TOOL_DESCRIPTIONS["pb_list_collections"],
    TOOL_SCHEMAS["pb_list_collections"],
)
async def pb_list_collections(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    try:
        cols = await _pb().list_collections()
        out = []
        for c in cols:
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
    except PBError as e:
        return _err(f"pb_list_collections failed: {e}")


@tool(
    "pb_search",
    TOOL_DESCRIPTIONS["pb_search"],
    TOOL_SCHEMAS["pb_search"],
)
async def pb_search(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb().list_page(
            args["collection"],
            filter=args.get("filter", ""),
            sort=args.get("sort", "-created"),
            expand=args.get("expand", ""),
            page=int(args.get("page", 1)),
            per_page=min(max(int(args.get("per_page", 30)), 1), 200),
        )
        return _ok(data)
    except PBError as e:
        return _err(f"pb_search failed: {e}")


@tool(
    "pb_get",
    TOOL_DESCRIPTIONS["pb_get"],
    TOOL_SCHEMAS["pb_get"],
)
async def pb_get(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb().get_record(
            args["collection"], args["id"], expand=args.get("expand", ""),
        )
        return _ok(data)
    except PBError as e:
        return _err(f"pb_get failed: {e}")


@tool(
    "pb_create",
    TOOL_DESCRIPTIONS["pb_create"],
    TOOL_SCHEMAS["pb_create"],
)
async def pb_create(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb().create_record(args["collection"], args["data"])
        _schedule_auto_sync(args["collection"])
        return _ok(data)
    except PBError as e:
        return _err(f"pb_create failed: {e}")


@tool(
    "pb_update",
    TOOL_DESCRIPTIONS["pb_update"],
    TOOL_SCHEMAS["pb_update"],
)
async def pb_update(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb().update_record(
            args["collection"], args["id"], args["data"],
        )
        _schedule_auto_sync(args["collection"])
        return _ok(data)
    except PBError as e:
        return _err(f"pb_update failed: {e}")


@tool(
    "pb_delete",
    TOOL_DESCRIPTIONS["pb_delete"],
    TOOL_SCHEMAS["pb_delete"],
)
async def pb_delete(args: dict[str, Any]) -> dict[str, Any]:
    try:
        await _pb().delete_record(args["collection"], args["id"])
        _schedule_auto_sync(args["collection"])
        return _ok({"ok": True, "collection": args["collection"], "deleted": args["id"]})
    except PBError as e:
        return _err(f"pb_delete failed: {e}")


@tool(
    "pb_get_collection",
    TOOL_DESCRIPTIONS["pb_get_collection"],
    TOOL_SCHEMAS["pb_get_collection"],
)
async def pb_get_collection(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb().get_collection(args["id_or_name"])
        return _ok(data)
    except PBError as e:
        return _err(f"pb_get_collection failed: {e}")


@tool(
    "pb_create_collection",
    TOOL_DESCRIPTIONS["pb_create_collection"],
    TOOL_SCHEMAS["pb_create_collection"],
)
async def pb_create_collection(args: dict[str, Any]) -> dict[str, Any]:
    try:
        body = {"name": args["name"], "type": args.get("type", "base"),
                "fields": args["fields"]}
        data = await _pb().create_collection(body)
        return _ok(data)
    except PBError as e:
        return _err(f"pb_create_collection failed: {e}")


@tool(
    "pb_update_collection",
    TOOL_DESCRIPTIONS["pb_update_collection"],
    TOOL_SCHEMAS["pb_update_collection"],
)
async def pb_update_collection(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _pb().update_collection(args["id_or_name"], args["patch"])
        return _ok(data)
    except PBError as e:
        return _err(f"pb_update_collection failed: {e}")


@tool(
    "pb_delete_collection",
    TOOL_DESCRIPTIONS["pb_delete_collection"],
    TOOL_SCHEMAS["pb_delete_collection"],
)
async def pb_delete_collection(args: dict[str, Any]) -> dict[str, Any]:
    try:
        await _pb().delete_collection(args["id_or_name"])
        return _ok({"ok": True, "deleted": args["id_or_name"]})
    except PBError as e:
        return _err(f"pb_delete_collection failed: {e}")


@tool(
    "smartnote_open_context",
    TOOL_DESCRIPTIONS["smartnote_open_context"],
    TOOL_SCHEMAS["smartnote_open_context"],
)
async def smartnote_open_context(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    try:
        data = await _pb().list_page(
            "claude_memos",
            filter="status='Active' && priority='High'",
            sort="-date", per_page=50,
        )
        return _ok(data)
    except PBError as e:
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
        sys.executable, "-m", "notion_sync.runner",
        "--force-now",
    ]
    coll = args.get("collection")
    if coll:
        cmd += ["--only", coll]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            cwd=str(BRIDGE_ROOT),
            capture_output=True, text=True, timeout=600,
        )
        log_path = str(SYNC_LOG)
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
    notion_token = settings.notion_token
    db_id        = settings.notion_sync_activity_db_id
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
        envelope = await _pb().list_page("sync_global", per_page=1)
        items = envelope.get("items", [])
        if not items:
            return _err("sync_global has no rows")
        row = items[0]
        await _pb().update_record("sync_global", row["id"], {"paused": True})
        return _ok({"ok": True, "paused": True})
    except PBError as e:
        return _err(f"sync_pause failed: {e}")


@tool(
    "sync_resume",
    "Resume the daily Notion ↔ PB sync runner after a pause. The next "
    "hourly cron tick will run normally (only at the configured local hour).",
    {},
)
async def sync_resume(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    try:
        envelope = await _pb().list_page("sync_global", per_page=1)
        items = envelope.get("items", [])
        if not items:
            return _err("sync_global has no rows")
        row = items[0]
        await _pb().update_record("sync_global", row["id"], {"paused": False})
        return _ok({"ok": True, "paused": False})
    except PBError as e:
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
