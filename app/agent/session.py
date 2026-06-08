"""Bridge session lifecycle — thin wrapper around SessionManager.

Kept as a stable module so callers (`app/api/sessions.py`, ws handler,
lifespan) don't have to change import paths. All real work delegates to
`app.agent.manager.manager`.
"""
from __future__ import annotations

import logging

import db

from app.agent.manager import manager
from app.state import state

log = logging.getLogger("bridge")


async def open_session(sid: str):
    """Load session from db + ensure manager has a live agent for it.
    Returns the agent (or None if session not found)."""
    sess = db.get_session(sid)
    if sess is None:
        log.warning("open_session: not found %s", sid)
        return None
    cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
    if not str(cwd).startswith(str(state.cwd_root)):
        cwd = state.cwd_root
    return await manager.get_or_create(
        sid, cwd=cwd,
        mode=sess.get("mode") or "code",
        model=sess.get("model") or "",
        sdk_session_id=sess.get("sdk_session_id"),
    )


async def new_session(cwd_rel: str | None = None,
                      mode: str = "code", model: str = "") -> str:
    """Create a new bridge session row + ensure a live agent for it."""
    from app.persistence.files import _resolve_in_root, _to_rel
    target_cwd = state.cwd_root
    if cwd_rel:
        resolved = _resolve_in_root(cwd_rel)
        if resolved and resolved.is_dir():
            target_cwd = resolved
    rel_cwd = _to_rel(target_cwd)
    sid = db.create_session(cwd=rel_cwd, title="", mode=mode, model=model)
    await manager.get_or_create(sid, cwd=target_cwd, mode=mode, model=model)
    return sid
