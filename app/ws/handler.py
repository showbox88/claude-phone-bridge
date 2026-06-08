"""WebSocket endpoint + message dispatch.

Exposes `router` (an APIRouter) carrying the single `/ws` WebSocket route.
HTTP middleware doesn't run on WS, so the cookie check happens inside
`ws_handler` against `auth_state` directly.

`handle_ws_message` switches on `msg.type`:
- user_message → spawn `run_user_turn` (Task 10's turn loop)
- permission_response → resolve the pending future + broadcast
- cmd → dispatch to `handle_cmd`
- ping → pong

`handle_cmd` covers the 9 client-driven commands (new/load/delete/rename/
switch-workspace/auto-approve/model/cwd/cancel).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import auth as auth_mod
import db

from app.agent.options import AVAILABLE_MODELS
from app.agent.session import init_client, new_session, open_session
from app.agent.turn import run_user_turn
from app.auth.state import auth_state
from app.persistence.files import _resolve_in_root, _to_rel, uploads_dir
from app.state import state
from app.ws.broadcast import broadcast

log = logging.getLogger("bridge")


def _recorder():
    # Lazy lookup so server.py's BRIDGE_RECORD-init runs first. Returns
    # the Recorder instance or None.
    import server
    return getattr(server, "_recorder", None)


router = APIRouter()


@router.websocket("/ws")
async def ws_handler(ws: WebSocket):
    # WebSocket bypasses HTTP middleware, so check the session cookie here.
    if auth_state.is_initialized():
        token = ws.cookies.get(auth_mod.COOKIE_NAME)
        if not token or auth_state.lookup_token(token) is None:
            # Standard policy violation close code; browser receives a clean reject.
            await ws.close(code=4401)
            return
    await ws.accept()
    rec = _recorder()
    if rec:
        rec.ws_open()
        _orig_send = ws.send_text
        _orig_recv = ws.receive_text

        async def _rec_send(text):
            await _orig_send(text)
            rec.ws_frame("out", text)

        async def _rec_recv():
            text = await _orig_recv()
            rec.ws_frame("in", text)
            return text

        ws.send_text = _rec_send  # type: ignore[method-assign]
        ws.receive_text = _rec_recv  # type: ignore[method-assign]
    state.websockets.add(ws)
    log.info("websocket connected (total=%d)", len(state.websockets))
    try:
        # send hello with current session snapshot
        hello: dict[str, Any] = {
            "type": "hello",
            "cwd": _to_rel(state.cwd),
            "session_id": state.session_id,
            "auto_approve": state.auto_approve,
        }
        if state.session_id:
            sess = db.get_session(state.session_id)
            if sess:
                hello["session"] = {
                    "id": sess["id"],
                    "title": sess["title"],
                    "cwd": sess["cwd"],
                    "mode": sess.get("mode") or "code",
                    "model": sess.get("model") or "",
                    "messages": sess["messages"],
                }
        # Replay any unanswered permission requests so a phone reconnecting
        # after a push-notification tap can render the card again.
        hello["pending_perms"] = [
            {"id": cid, "tool": meta.get("tool"), "input": meta.get("input")}
            for cid, meta in state.pending_meta.items()
            if cid in state.pending and not state.pending[cid].done()
        ]
        await ws.send_text(json.dumps(hello, ensure_ascii=False))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "msg": "invalid JSON"}))
                continue
            await handle_ws_message(ws, msg)
    except WebSocketDisconnect:
        pass
    finally:
        state.websockets.discard(ws)
        if rec:
            rec.ws_close(None)
        log.info("websocket closed (remaining=%d)", len(state.websockets))


async def handle_ws_message(ws: WebSocket, msg: dict) -> None:
    t = msg.get("type")
    if t == "user_message":
        text = (msg.get("text") or "").strip()
        images = msg.get("images") or []
        files = msg.get("files") or []
        client_tz = (msg.get("client_tz") or "").strip()
        if client_tz:
            state.client_tz = client_tz
        if not text and not images and not files:
            return
        await broadcast({
            "type": "user_echo", "text": text, "images": images, "files": files,
        })
        state.current_turn_task = asyncio.create_task(run_user_turn(text, images, files))
    elif t == "permission_response":
        cb_id = msg.get("id")
        decision = msg.get("decision")
        fut = state.pending.get(cb_id) if cb_id else None
        if fut and not fut.done():
            fut.set_result(decision)
            # Tell every connected client (other phone tab, desktop browser, etc.)
            # so their permission cards flip to the resolved state in sync.
            await broadcast({
                "type": "permission_resolved",
                "id": cb_id,
                "decision": decision,
            })
    elif t == "cmd":
        await handle_cmd(msg)
    elif t == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))


async def handle_cmd(msg: dict) -> None:
    name = msg.get("name")
    if name == "new_session":
        mode = msg.get("mode")
        if mode not in ("code", "chat"):
            mode = "code"
        await new_session(cwd_rel=msg.get("cwd"), mode=mode)
    elif name == "load_session":
        sid = msg.get("id")
        if sid:
            await open_session(sid)
    elif name == "delete_session":
        sid = msg.get("id")
        if not sid:
            return
        # reuse REST handler logic
        if db.get_session(sid):
            db.delete_session(sid)
            sdir = uploads_dir() / sid
            if sdir.is_dir():
                with contextlib.suppress(OSError):
                    shutil.rmtree(sdir)
            if state.session_id == sid:
                latest = db.latest_session_id()
                if latest:
                    await open_session(latest)
                else:
                    await new_session()
            await broadcast({"type": "session_deleted", "id": sid})
    elif name == "rename_session":
        sid = msg.get("id"); title = msg.get("title")
        if sid and title is not None:
            db.update_session(sid, title=str(title)[:80])
            await broadcast({"type": "session_renamed", "id": sid, "title": title})
    elif name == "switch_workspace":
        # Switch to most recent session of the requested mode, or create a new one.
        # This is the "Chat ↔ Code" toggle; sessions stay strictly typed.
        new_mode = msg.get("mode")
        if new_mode not in ("code", "chat"):
            return
        target_sid = db.latest_session_id(mode=new_mode)
        if target_sid:
            await open_session(target_sid)
        else:
            await new_session(mode=new_mode)
    elif name == "set_auto_approve":
        new_val = bool(msg.get("value"))
        if new_val == state.auto_approve:
            return
        state.auto_approve = new_val
        await broadcast({
            "type": "auto_approve_changed",
            "value": state.auto_approve,
        })
        await broadcast({
            "type": "system",
            "msg": ("🚀 自动批准已开启 — 后续工具调用不再询问"
                    if state.auto_approve
                    else "🛑 自动批准已关闭 — 恢复逐次询问"),
        })
    elif name == "set_model":
        new_model = msg.get("model") or ""
        valid_ids = {m["id"] for m in AVAILABLE_MODELS}
        if new_model not in valid_ids:
            return
        if not state.session_id or new_model == state.model:
            return
        state.model = new_model
        db.update_session(state.session_id, model=new_model)
        await init_client(resume_sdk_id=state.sdk_session_id)
        await broadcast({"type": "session_model_changed", "id": state.session_id, "model": new_model})
    elif name == "cwd":
        rel = msg.get("path", "")
        new_cwd = _resolve_in_root(rel)
        if new_cwd is None or not new_cwd.is_dir():
            await broadcast({"type": "error", "msg": f"invalid cwd: {rel}"})
            return
        state.cwd = new_cwd
        if state.session_id:
            db.update_session(state.session_id, cwd=_to_rel(new_cwd))
        await init_client(resume_sdk_id=state.sdk_session_id)
    elif name == "cancel":
        task = state.current_turn_task
        if task and not task.done():
            task.cancel()
        else:
            await broadcast({"type": "system", "msg": "nothing to cancel"})
