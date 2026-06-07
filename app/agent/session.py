"""Claude SDK session lifecycle.

- `init_client(resume_sdk_id)` (re)constructs `state.client` with current
  options. Old client disconnects best-effort.
- `open_session(sid)` switches the active bridge session: loads it from db,
  resolves cwd/mode/model, reconnects the SDK with the stored sdk_session_id,
  broadcasts `session_loaded`.
- `new_session(cwd_rel, mode, model)` creates a new bridge session row in
  db, opens it.
"""
from __future__ import annotations

import contextlib
import logging

from claude_agent_sdk import ClaudeSDKClient

import db

from app.agent.options import make_options
from app.persistence.files import _resolve_in_root, _to_rel
from app.state import state
from app.ws.broadcast import broadcast

log = logging.getLogger("bridge")


async def init_client(resume_sdk_id: str | None = None) -> None:
    if state.client is not None:
        with contextlib.suppress(Exception):
            await state.client.disconnect()
        state.client = None

    log.info(
        "starting Claude session cwd=%s resume=%s mode=%s model=%s",
        state.cwd, resume_sdk_id, state.mode, state.model or "default",
    )
    state.client = ClaudeSDKClient(options=make_options(resume_sdk_id))
    await state.client.connect()
    state.sdk_session_id = resume_sdk_id  # may be overwritten by next ResultMessage
    await broadcast({
        "type": "system",
        "msg": f"session ready · {state.mode} · {state.model or 'default'} · cwd={_to_rel(state.cwd) or '/'}",
    })


async def open_session(sid: str) -> None:
    """Switch active session and (re)connect the SDK client, resuming if possible."""
    sess = db.get_session(sid)
    if sess is None:
        await broadcast({"type": "error", "msg": f"session not found: {sid}"})
        return
    state.session_id = sid
    state.cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
    if not str(state.cwd).startswith(str(state.cwd_root)):
        state.cwd = state.cwd_root
    state.mode = sess.get("mode") or "code"
    state.model = sess.get("model") or ""
    await init_client(resume_sdk_id=sess.get("sdk_session_id"))
    await broadcast({
        "type": "session_loaded",
        "session": {
            "id": sess["id"],
            "title": sess["title"],
            "cwd": _to_rel(state.cwd),
            "mode": state.mode,
            "model": state.model,
            "messages": sess["messages"],
        },
    })


async def new_session(cwd_rel: str | None = None, mode: str = "code", model: str = "") -> str:
    target_cwd = state.cwd_root
    if cwd_rel:
        resolved = _resolve_in_root(cwd_rel)
        if resolved and resolved.is_dir():
            target_cwd = resolved
    rel_cwd = _to_rel(target_cwd)
    sid = db.create_session(cwd=rel_cwd, title="", mode=mode, model=model)
    await open_session(sid)
    return sid
