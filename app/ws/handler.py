"""WebSocket endpoint + message dispatch.

Each WS connection binds to a single session_id (from db.latest_session_id()
on accept, or via cmd:load_session). All session-specific events fan out
only to WSs bound to that session.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import auth as auth_mod
import db

from app.agent.manager import manager
from app.agent.options import AVAILABLE_MODELS
from app.agent.turn import run_user_turn
from app.auth.state import auth_state
from app.log import get_logger
from app.persistence.files import _resolve_in_root, _to_rel, uploads_dir
from app.state import state
from app.ws.broadcast import broadcast, broadcast_to_agent

log = get_logger("bridge")
router = APIRouter()


async def _ensure_agent_for_ws(ws: WebSocket):
    """Pick a default session for this WS and ensure an agent exists.
    Sets state.ws_sessions[ws] = sid. Returns the agent (or None on db error)."""
    sid = db.latest_session_id()
    if not sid:
        from app.agent.session import new_session
        sid = await new_session()
    sess = db.get_session(sid)
    if not sess:
        return None
    cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
    if not str(cwd).startswith(str(state.cwd_root)):
        cwd = state.cwd_root
    agent = await manager.get_or_create(
        sid, cwd=cwd,
        mode=sess.get("mode") or "code",
        model=sess.get("model") or "",
        sdk_session_id=sess.get("sdk_session_id"),
    )
    state.ws_sessions[ws] = sid
    return agent


@router.websocket("/ws")
async def ws_handler(ws: WebSocket):
    if auth_state.is_initialized():
        token = ws.cookies.get(auth_mod.COOKIE_NAME)
        if not token or auth_state.lookup_token(token) is None:
            await ws.close(code=4401)
            return
    await ws.accept()
    state.websockets.add(ws)
    log.info("websocket connected (total=%d)", len(state.websockets))
    try:
        agent = await _ensure_agent_for_ws(ws)
        hello: dict[str, Any] = {
            "type": "hello",
            "cwd": _to_rel(agent.cwd) if agent else "",
            "session_id": agent.session_id if agent else None,
            "auto_approve": state.auto_approve,
        }
        if agent and agent.session_id:
            sess = db.get_session(agent.session_id)
            if sess:
                hello["session"] = {
                    "id": sess["id"], "title": sess["title"],
                    "cwd": sess["cwd"],
                    "mode": sess.get("mode") or "code",
                    "model": sess.get("model") or "",
                    "messages": sess["messages"],
                }
        sid = agent.session_id if agent else None
        hello["pending_perms"] = [
            {"id": cid, "tool": meta.get("tool"), "input": meta.get("input")}
            for cid, meta in state.pending_meta.items()
            if cid in state.pending and not state.pending[cid].done()
            and (meta.get("session_id") in (None, sid))
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
        state.ws_sessions.pop(ws, None)
        log.info("websocket closed (remaining=%d)", len(state.websockets))


def _agent_for_ws(ws: WebSocket):
    sid = state.ws_sessions.get(ws)
    return manager.get(sid) if sid else None


async def handle_ws_message(ws: WebSocket, msg: dict) -> None:
    t = msg.get("type")
    if t == "user_message":
        text = (msg.get("text") or "").strip()
        images = msg.get("images") or []
        files = msg.get("files") or []
        client_tz = (msg.get("client_tz") or "").strip()
        agent = _agent_for_ws(ws)
        if agent is None:
            await ws.send_text(json.dumps(
                {"type": "error", "msg": "no active session"}))
            return
        if client_tz:
            agent.client_tz = client_tz
        if not text and not images and not files:
            return
        await broadcast_to_agent(agent, {
            "type": "user_echo", "text": text,
            "images": images, "files": files,
        })
        agent.current_turn_task = asyncio.create_task(
            run_user_turn(agent, text, images, files))
    elif t == "permission_response":
        cb_id = msg.get("id")
        decision = msg.get("decision")
        fut = state.pending.get(cb_id) if cb_id else None
        if fut and not fut.done():
            fut.set_result(decision)
            meta = state.pending_meta.get(cb_id, {})
            sid_for_msg = meta.get("session_id")
            payload = {"type": "permission_resolved",
                       "id": cb_id, "decision": decision}
            if sid_for_msg:
                ag = manager.get(sid_for_msg)
                if ag is not None:
                    await broadcast_to_agent(ag, payload)
                else:
                    await broadcast(payload)
            else:
                await broadcast(payload)
    elif t == "cmd":
        await handle_cmd(ws, msg)
    elif t == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))


async def handle_cmd(ws: WebSocket, msg: dict) -> None:
    name = msg.get("name")
    if name == "new_session":
        mode = msg.get("mode") if msg.get("mode") in ("code", "chat") else "code"
        from app.agent.session import new_session
        sid = await new_session(cwd_rel=msg.get("cwd"), mode=mode)
        state.ws_sessions[ws] = sid
        agent = manager.get(sid)
        if agent:
            await broadcast_to_agent(agent, {
                "type": "session_loaded",
                "session": _session_payload(sid, agent),
            })
    elif name == "load_session":
        sid = msg.get("id")
        if not sid: return
        sess = db.get_session(sid)
        if not sess:
            await ws.send_text(json.dumps(
                {"type": "error", "msg": f"session not found: {sid}"}))
            return
        cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
        if not str(cwd).startswith(str(state.cwd_root)):
            cwd = state.cwd_root
        agent = await manager.get_or_create(
            sid, cwd=cwd,
            mode=sess.get("mode") or "code",
            model=sess.get("model") or "",
            sdk_session_id=sess.get("sdk_session_id"),
        )
        state.ws_sessions[ws] = sid
        await ws.send_text(json.dumps({
            "type": "session_loaded",
            "session": _session_payload(sid, agent),
        }, ensure_ascii=False))
    elif name == "delete_session":
        sid = msg.get("id")
        if not sid: return
        if db.get_session(sid):
            await manager.destroy(sid)
            db.delete_session(sid)
            sdir = uploads_dir() / sid
            if sdir.is_dir():
                with contextlib.suppress(OSError):
                    shutil.rmtree(sdir)
            for w, bound in list(state.ws_sessions.items()):
                if bound == sid:
                    latest = db.latest_session_id()
                    if latest:
                        latest_sess = db.get_session(latest)
                        if latest_sess:
                            cwd = (state.cwd_root / latest_sess["cwd"]).resolve() if latest_sess["cwd"] else state.cwd_root
                            await manager.get_or_create(
                                latest, cwd=cwd,
                                mode=latest_sess.get("mode") or "code",
                                model=latest_sess.get("model") or "",
                                sdk_session_id=latest_sess.get("sdk_session_id"),
                            )
                            state.ws_sessions[w] = latest
                    else:
                        state.ws_sessions.pop(w, None)
            await broadcast({"type": "session_deleted", "id": sid})
    elif name == "rename_session":
        sid = msg.get("id"); title = msg.get("title")
        if sid and title is not None:
            db.update_session(sid, title=str(title)[:80])
            await broadcast({"type": "session_renamed",
                             "id": sid, "title": title})
    elif name == "switch_workspace":
        new_mode = msg.get("mode")
        if new_mode not in ("code", "chat"): return
        target_sid = db.latest_session_id(mode=new_mode)
        if target_sid:
            sess = db.get_session(target_sid)
            cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
            agent = await manager.get_or_create(
                target_sid, cwd=cwd, mode=sess.get("mode") or "code",
                model=sess.get("model") or "",
                sdk_session_id=sess.get("sdk_session_id"),
            )
            state.ws_sessions[ws] = target_sid
            await ws.send_text(json.dumps({
                "type": "session_loaded",
                "session": _session_payload(target_sid, agent),
            }, ensure_ascii=False))
        else:
            from app.agent.session import new_session
            sid = await new_session(mode=new_mode)
            state.ws_sessions[ws] = sid
    elif name == "set_auto_approve":
        new_val = bool(msg.get("value"))
        if new_val == state.auto_approve: return
        state.auto_approve = new_val
        await broadcast({
            "type": "auto_approve_changed", "value": state.auto_approve,
        })
        await broadcast({
            "type": "system",
            "msg": ("🚀 自动批准已开启 — 后续工具调用不再询问"
                    if state.auto_approve
                    else "🛑 自动批准已关闭 — 恢复逐次询问"),
        })
    elif name == "set_model":
        new_model = msg.get("model") or ""
        if new_model not in {m["id"] for m in AVAILABLE_MODELS}:
            return
        agent = _agent_for_ws(ws)
        if agent is None or new_model == agent.model: return
        db.update_session(agent.session_id, model=new_model)
        await manager.recreate(agent.session_id, model=new_model)
        await broadcast_to_agent(agent, {
            "type": "session_model_changed",
            "id": agent.session_id, "model": new_model,
        })
    elif name == "cwd":
        rel = msg.get("path", "")
        new_cwd = _resolve_in_root(rel)
        if new_cwd is None or not new_cwd.is_dir():
            await ws.send_text(json.dumps(
                {"type": "error", "msg": f"invalid cwd: {rel}"}))
            return
        agent = _agent_for_ws(ws)
        if agent is None: return
        db.update_session(agent.session_id, cwd=_to_rel(new_cwd))
        await manager.recreate(agent.session_id, cwd=new_cwd)
    elif name == "cancel":
        agent = _agent_for_ws(ws)
        if agent is None: return
        task = agent.current_turn_task
        if task and not task.done():
            task.cancel()
        else:
            await broadcast_to_agent(agent,
                {"type": "system", "msg": "nothing to cancel"})


def _session_payload(sid: str, agent) -> dict:
    sess = db.get_session(sid)
    return {
        "id": sid,
        "title": sess.get("title", "") if sess else "",
        "cwd": _to_rel(agent.cwd),
        "mode": agent.mode,
        "model": agent.model,
        "messages": sess["messages"] if sess else [],
    }
