"""Sync routes: manual trigger + sync_config registry CRUD + snapshot export."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import notion_sync.config as sync_config_registry
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.provisioner import provision_notion_db

from app.paths import BRIDGE_ROOT

log = logging.getLogger("bridge")
router = APIRouter()

_SYSTEM_PB_COLLECTIONS = {
    "sync_config", "sync_global",
    "_pb_users_auth_", "_superusers", "_mfas", "_otps",
    "_authOrigins", "_externalAuths",
}


def _sync_log_path() -> Path:
    return BRIDGE_ROOT / ".bridge_data" / "sync.log"


def _latest_run_end_summary() -> dict[str, Any]:
    """Return the most recent `run_end` JSON line from sync.log, or {}."""
    p = _sync_log_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return {}
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if evt.get("event") == "run_end":
            return {k: v for k, v in evt.items() if k != "event"}
    return {}


def _pb_collection_field_names(pb: PBClient, name: str) -> set[str]:
    """Field names of one PB collection. Raises if not found."""
    raw = pb._http("GET", f"/api/collections/{name}")  # noqa: SLF001
    return {f["name"] for f in raw.get("fields", [])}


@router.post("/api/sync/now")
async def api_sync_now(body: dict | None = None):
    """Trigger notion_sync.runner --force-now. Returns the run_end summary."""
    body = body or {}
    only = (body.get("collection") or "").strip() or None
    cmd = [sys.executable, "-m", "notion_sync.runner", "--force-now"]
    if only:
        cmd += ["--only", only]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BRIDGE_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(504, "sync runner timed out after 10 min")
    except FileNotFoundError as e:
        raise HTTPException(500, f"runner not found: {e}")
    summary = _latest_run_end_summary()
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "summary": summary,
        "stderr": (stderr or b"").decode("utf-8", "replace")[-400:],
    }


@router.get("/api/sync/targets")
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


@router.post("/api/sync/targets")
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
            sys.executable,
            "scripts/reconcile_initial.py", "--only", collection,
            cwd=str(BRIDGE_ROOT),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=600)
    except Exception as e:
        log.warning("reconcile_initial spawn for %s failed: %s", collection, e)


@router.patch("/api/sync/targets/{collection}")
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


@router.delete("/api/sync/targets/{collection}")
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


@router.post("/api/sync/registry/export-snapshot")
async def api_sync_registry_export_snapshot():
    """Run scripts/dump_sync_registry.py and return the output path."""
    out_path = "notion_sync/registry.snapshot.yaml"
    cmd = [sys.executable,
            "scripts/dump_sync_registry.py", "--path", out_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(BRIDGE_ROOT),
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
