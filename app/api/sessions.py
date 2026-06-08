"""Bridge-session CRUD routes (5).

Backed by db.* SQLite. Delete also tears down the uploads dir for that
session and, if it was the active session, switches to whatever became
latest_session_id (or spins up a fresh one).
"""
from __future__ import annotations

import contextlib
import shutil
from typing import Any

from fastapi import APIRouter, HTTPException

import db

from app.agent.session import new_session, open_session
from app.persistence.files import uploads_dir
from app.state import state

router = APIRouter()


@router.get("/api/sessions")
async def api_sessions_list(q: str = ""):
    return {
        "current": state.session_id,
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
    db.delete_session(sid)
    # remove uploads dir for this session
    sdir = uploads_dir() / sid
    if sdir.is_dir():
        with contextlib.suppress(OSError):
            shutil.rmtree(sdir)
    if state.session_id == sid:
        # spin up a new session as current
        latest = db.latest_session_id()
        if latest:
            await open_session(latest)
        else:
            await new_session()
    return {"ok": True, "current": state.session_id}
