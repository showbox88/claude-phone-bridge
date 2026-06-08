"""Read-only meta routes: /api/health, /api/usage, /api/meta."""
from __future__ import annotations

import socket

from fastapi import APIRouter, Request

import db

from app.agent.manager import manager
from app.agent.options import AVAILABLE_MODELS, AVAILABLE_MODES
from app.auth.middleware import _current_device
from app.auth.state import auth_state
from app.settings import settings
from app.state import state

router = APIRouter()


@router.get("/api/health")
async def api_health(request: Request):
    """Lightweight probe. Returns minimal info to anonymous clients (so the
    dashboard's HTTP probe still works without auth) and full session info
    only to authenticated devices."""
    base = {"ok": True}
    if auth_state.is_initialized() and _current_device(request) is None:
        return base
    base.update({
        "name": settings.bridge_name or socket.gethostname(),
        "cwd_root": str(state.cwd_root).replace("\\", "/"),
        "active_sessions": manager.active_ids(),
    })
    latest_sid = db.latest_session_id()
    if latest_sid:
        agent = manager.get(latest_sid)
        if agent:
            base.update({
                "session_id": agent.session_id,
                "mode": agent.mode,
                "model": agent.model or "",
            })
    return base


@router.get("/api/usage")
async def api_usage():
    return db.usage_summary()


@router.get("/api/meta")
async def api_meta():
    """Static metadata for the UI: available modes & models."""
    return {"modes": AVAILABLE_MODES, "models": AVAILABLE_MODELS}
