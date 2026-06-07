"""FastAPI bridge: phone web UI <-> local Claude Code session.

Sessions persist in SQLite; each bridge session maps to an SDK session_id so
"continue history" works across restarts via SDK's `resume` option. Images
upload to .bridge_uploads/<session_id>/ and ride along as multimodal content.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    FastAPI, HTTPException, Request, Response,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import socket

import auth as auth_mod

import db
import push
import report


from app.integrations.pb import (
    PBClient,
    PBError,
    refresh_token_into_env,
)
from app.paths import AUTH_FILE, BRIDGE_ROOT
from app.settings import settings

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# ============================================================================
# PocketBase (Smart Note 打卡子系统) — local-first canonical data store.
# Claude SDK reaches it via Bash + curl using the auto-refreshed PB_TOKEN env.
# Token is fetched at startup and refreshed every 30 min.
# ============================================================================
import urllib.request, urllib.error, urllib.parse  # noqa: E401
import datetime as _dt
import hashlib as _hashlib

POCKETBASE_URL = settings.pocketbase_url
POCKETBASE_ADMIN_EMAIL = settings.pocketbase_admin_email
POCKETBASE_ADMIN_PASSWORD = settings.pocketbase_admin_password

# Unified PB client. Shared by:
#  - _pb_refresh_token (12h loop + lifespan startup) via refresh_token_into_env
#  - _pb_get_json (today-todos endpoint)
# The sync_config / sync_global REST handlers further down still use
# notion_sync.pb_api.PBClient (a shim around the same unified class) —
# separate instance, same superuser creds, independent PB admin tokens.
_pb_instance: PBClient | None = None


def _pb_client() -> PBClient:
    global _pb_instance
    if _pb_instance is None:
        _pb_instance = PBClient(
            POCKETBASE_URL,
            POCKETBASE_ADMIN_EMAIL,
            POCKETBASE_ADMIN_PASSWORD,
        )
    return _pb_instance


def _pb_refresh_token() -> bool:
    """Auth against PocketBase and mirror PB_TOKEN/PB_URL into os.environ.

    The os.environ mirror is the side-channel for child Bash subprocesses
    spawned by the Claude SDK — the CHAT-mode CHECKIN flow uses
    `$PB_TOKEN` and `$PB_URL` directly in curl commands. See
    refresh_token_into_env() in app/integrations/pb/token.py.

    Returns True on success, False if creds missing or auth failed.
    """
    if not (POCKETBASE_URL and POCKETBASE_ADMIN_EMAIL and POCKETBASE_ADMIN_PASSWORD):
        return False
    try:
        refresh_token_into_env(_pb_client())
        return True
    except PBError as e:
        log.error("PB token refresh failed: %s", e)
        return False


async def _pb_refresh_loop() -> None:
    """Re-auth every 12 h so PB_TOKEN never expires under Claude's feet."""
    while True:
        try:
            await asyncio.sleep(43200)
            await asyncio.to_thread(_pb_refresh_token)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception("PB refresh loop error: %s", e)


from app.agent.permission import AUTO_ALLOW, CHAT_TOOLS, can_use_tool, summarize_input, truncate  # noqa: E402,F401
from app.agent.options import (  # noqa: E402,F401
    AVAILABLE_MODELS,
    AVAILABLE_MODES,
    CHAT_SYSTEM_PROMPT,
    PB_MCP_SERVER,
    make_options,
)

from app.state import state  # noqa: E402  — AppState dataclass lives in app/state.py now
from app.persistence.files import (  # noqa: E402,F401  — re-export keeps existing call sites working
    UPLOAD_DIRNAME,
    MAX_IMAGES_PER_MESSAGE,
    MAX_UPLOAD_BYTES,
    ALLOWED_IMAGE_MIMES,
    ALLOWED_DOC_MIMES,
    TEXT_EXTS,
    SHEET_EXTS,
    MAX_TEXT_INLINE_CHARS,
    MAX_SHEET_ROWS_PER_SHEET,
    uploads_dir,
    _resolve_in_root,
    _to_rel,
    classify_upload,
    _safe_filename,
)
from app.ws.broadcast import broadcast  # noqa: E402


from app.reporting.weekly_report import _weekly_report_posted  # noqa: E402
from app.agent.session import init_client, open_session, new_session  # noqa: E402
from app.agent.content import _build_user_content, _read_text_safe, _read_xlsx_as_text  # noqa: E402,F401
from app.agent.turn import _block_to_event, _save_msg, run_user_turn  # noqa: E402,F401


# ---------- FastAPI ----------

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    # Resolve cwd_root from settings before anything else touches it.
    # app/state.py uses Path.cwd().resolve() as the dataclass default to
    # avoid importing app.settings at module-load time; lifespan corrects it
    # here. This will move into app/main.py:lifespan in Task 14.
    state.cwd_root = Path(settings.default_cwd or os.getcwd()).resolve()
    state.cwd = state.cwd_root
    push.init()
    db.init(state.cwd_root / ".bridge_data" / "bridge.db")
    uploads_dir()
    # PocketBase: fetch initial token + spawn background refresh loop.
    pb_ready = _pb_refresh_token()
    pb_task = asyncio.create_task(_pb_refresh_loop()) if pb_ready else None
    if not pb_ready and POCKETBASE_URL:
        log.warning("PocketBase configured but initial auth failed — 打卡 will not work")
    report_task = asyncio.create_task(
        report.scheduler_loop(str(state.cwd_root), on_post=_weekly_report_posted)
    )
    try:
        latest = db.latest_session_id()
        if latest:
            await open_session(latest)
        else:
            await new_session()
    except Exception as e:
        log.exception("initial Claude session failed: %s", e)
    yield
    if pb_task is not None:
        pb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pb_task
    report_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await report_task
    if state.client is not None:
        with contextlib.suppress(Exception):
            await state.client.disconnect()


app = FastAPI(lifespan=lifespan)

# --- Phase 2 baseline recorder (BRIDGE_RECORD=1) ----------------------------
# Removed at end of Phase 2 (Task 16 of phase-2-app-package plan).
_recorder = None
if os.environ.get("BRIDGE_RECORD"):
    from pathlib import Path as _RP
    import sys as _RS
    _RS.path.insert(0, str(_RP(__file__).resolve().parent / "tests"))
    from replay import Recorder as _Recorder  # noqa: E402
    _recorder = _Recorder(_RP(os.environ.get(
        "BRIDGE_RECORD_PATH",
        "tests/fixtures/phase2_baseline.jsonl")))


@app.middleware("http")
async def _record_http(request: Request, call_next):
    if not _recorder:
        return await call_next(request)
    req_body = await request.body()

    async def _receive():
        return {"type": "http.request", "body": req_body, "more_body": False}

    request._receive = _receive
    response = await call_next(request)
    chunks = []
    async for c in response.body_iterator:
        chunks.append(c)
    resp_body = b"".join(chunks)
    _recorder.http(request.method, request.url.path,
                   str(request.url.query), req_body,
                   response.status_code, resp_body)
    from starlette.responses import Response as _SR
    return _SR(content=resp_body, status_code=response.status_code,
               headers=dict(response.headers),
               media_type=response.media_type)
# --- end Phase 2 baseline recorder ------------------------------------------

# Allow cross-origin requests so a phone-side PWA loaded from any PC can talk
# to any other PC's bridge over Tailscale. The user's auth/security model is
# Tailscale tailnet itself (only your own devices can route to these hosts).
_origins_env = settings.allowed_origins
_allowed_origins = ["*"] if _origins_env.strip() == "*" else [
    o.strip() for o in _origins_env.split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,    # cookies are required for session auth
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ============================================================================
# Authentication: password + TOTP, persistent device-bound sessions.
# Public endpoints: /login /logout /setup /setup/verify /static/* /sw.js
#                   /manifest.json /icon.svg /api/health /api/vapid-public-key
# Everything else (API, WebSocket, /uploads, /) requires a valid bridge_session
# cookie. WebSocket auth is enforced inside the /ws handler.
# ============================================================================

from app.auth.state import _COOKIE_SECONDS, auth_state  # noqa: E402

from app.auth.middleware import auth_middleware, _current_device  # noqa: E402,F401
from app.auth.pages import pages_router  # noqa: E402

app.middleware("http")(auth_middleware)
app.include_router(pages_router)

# API routers (Task 12).
from app.api.meta import router as _meta_router  # noqa: E402
from app.api.well_known import router as _well_known_router  # noqa: E402
from app.api.push import router as _push_router  # noqa: E402
from app.api.today_todos import router as _today_todos_router  # noqa: E402
from app.api.browse import router as _browse_router  # noqa: E402
from app.api.sessions import router as _sessions_router  # noqa: E402
from app.api.uploads import router as _uploads_router  # noqa: E402
from app.api.settings import router as _settings_router  # noqa: E402
from app.api.sync import router as _sync_router  # noqa: E402
from app.api.poi import router as _poi_router  # noqa: E402

app.include_router(_meta_router)
app.include_router(_well_known_router)
app.include_router(_push_router)
app.include_router(_today_todos_router)
app.include_router(_browse_router)
app.include_router(_sessions_router)
app.include_router(_uploads_router)
app.include_router(_settings_router)
app.include_router(_sync_router)
app.include_router(_poi_router)



# ---------- WebSocket ----------

@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    # WebSocket bypasses HTTP middleware, so check the session cookie here.
    if auth_state.is_initialized():
        token = ws.cookies.get(auth_mod.COOKIE_NAME)
        if not token or auth_state.lookup_token(token) is None:
            # Standard policy violation close code; browser receives a clean reject.
            await ws.close(code=4401)
            return
    await ws.accept()
    if _recorder:
        _recorder.ws_open()
        _orig_send = ws.send_text
        _orig_recv = ws.receive_text

        async def _rec_send(text):
            await _orig_send(text)
            _recorder.ws_frame("out", text)

        async def _rec_recv():
            text = await _orig_recv()
            _recorder.ws_frame("in", text)
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
        if _recorder:
            _recorder.ws_close(None)
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


# ---------- static files ----------

STATIC_DIR = Path(__file__).parent / "static"


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/")
async def index():
    # Never let the browser cache index.html — it's the only file that pins
    # the version string for /static/app.js & style.css. If it's stale, the
    # phone keeps loading old JS forever.
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers=_NO_CACHE,
    )


@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _mount_uploads():
    udir = uploads_dir()
    app.mount("/uploads", StaticFiles(directory=str(udir)), name="uploads")


_mount_uploads()


# ---------- entry ----------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
