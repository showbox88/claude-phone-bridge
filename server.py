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
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    FastAPI, HTTPException, Request, Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import socket

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

from app.ws.handler import router as _ws_router  # noqa: E402
app.include_router(_ws_router)



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
