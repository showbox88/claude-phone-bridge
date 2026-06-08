"""Bridge-session CRUD routes (5).

Backed by db.* SQLite. Delete also destroys any live ClaudeAgent for the
session (via the manager) and tears down its uploads dir. Spinning up a
replacement session is the WS handler's job (cmd:delete_session).
"""
from __future__ import annotations

import contextlib
import shutil
from typing import Any

from fastapi import APIRouter, HTTPException

import db

from app.agent.manager import manager
from app.agent.session import new_session
from app.persistence.files import uploads_dir

router = APIRouter()


@router.get("/api/sessions")
async def api_sessions_list(q: str = ""):
    return {
        "current": db.latest_session_id(),
        "sessions": db.search_sessions(q) if q.strip() else db.list_sessions(),
        "query": q,
    }


@router.post("/api/sessions")
async def api_sessions_create(body: dict | None = None):
    body = body or {}
    sid = await new_session(
        cwd_rel=body.get("cwd"),
        mode=body.get("mode") or "code",
        model=body.get("model") or "",
    )
    return {"id": sid}


@router.get("/api/sessions/{sid}")
async def api_sessions_get(sid: str):
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    return sess


@router.patch("/api/sessions/{sid}")
async def api_sessions_patch(sid: str, body: dict):
    upd: dict[str, Any] = {}
    if "title" in body: upd["title"] = str(body["title"])[:80]
    if "mode" in body and body["mode"] in ("code", "chat"): upd["mode"] = body["mode"]
    if "model" in body: upd["model"] = str(body["model"])[:32]
    if not upd:
        raise HTTPException(400, "nothing to update")
    db.update_session(sid, **upd)
    return {"ok": True}


@router.delete("/api/sessions/{sid}")
async def api_sessions_delete(sid: str):
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    await manager.destroy(sid)
    db.delete_session(sid)
    sdir = uploads_dir() / sid
    if sdir.is_dir():
        with contextlib.suppress(OSError):
            shutil.rmtree(sdir)
    return {"ok": True, "current": db.latest_session_id()}
