"""FastAPI entrypoint — the application object lives here.

server.py is a 16-line shim that re-exports `app` so the systemd unit's
`uvicorn server:app` ExecStart keeps working without edits. New code
should import directly from `app.main`.

Assembled at import time:
1. PB token + refresh loop helpers (`_pb_client`, `_pb_refresh_token`,
   `_pb_refresh_loop`) — kept here because they're used by the lifespan
   and by `app.api.today_todos` (lazy `from server import _pb_client`,
   which after the shim resolves to `app.main._pb_client`).
2. FastAPI `app` with `lifespan` that resolves cwd_root from
   settings.default_cwd BEFORE anything else, then starts push, db,
   PB token loop, weekly-report scheduler, default session.
3. The Phase 2 baseline recorder (BRIDGE_RECORD=1 gated). Removed in
   Task 16.
4. CORS, auth middleware, auth pages, 10 API routers, ws handler,
   static-file mounts.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db
import push
import report

from app.integrations.pb import PBClient, PBError, refresh_token_into_env
from app.paths import BRIDGE_ROOT
from app.persistence.files import uploads_dir
from app.settings import settings
from app.state import state

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# ============================================================================
# PocketBase token bootstrap. PB_TOKEN/PB_URL are mirrored into os.environ
# so the Claude SDK's child Bash subprocesses can curl PB without per-call
# auth. _pb_client() is also imported lazily by app.api.today_todos.
# ============================================================================

POCKETBASE_URL = settings.pocketbase_url
_pb_instance: PBClient | None = None


def _pb_client() -> PBClient:
    global _pb_instance
    if _pb_instance is None:
        _pb_instance = PBClient(
            POCKETBASE_URL,
            settings.pocketbase_admin_email,
            settings.pocketbase_admin_password,
        )
    return _pb_instance


def _pb_refresh_token() -> bool:
    if not (POCKETBASE_URL and settings.pocketbase_admin_email
            and settings.pocketbase_admin_password):
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


# Side-effect import: builds PB_MCP_SERVER at module load (safe-guarded).
from app.agent.options import PB_MCP_SERVER  # noqa: E402, F401
from app.agent.session import new_session, open_session  # noqa: E402
from app.reporting.weekly_report import _weekly_report_posted  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    # Resolve cwd_root from settings before anything else touches it.
    # app/state.py uses Path.cwd().resolve() as the dataclass default to
    # avoid importing app.settings at module-load time; lifespan corrects it.
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
# Removed at end of Phase 2 (Task 16 of the phase-2-app-package plan).
_recorder = None
if os.environ.get("BRIDGE_RECORD"):
    import sys as _RS
    _RS.path.insert(0, str(BRIDGE_ROOT / "tests"))
    from replay import Recorder as _Recorder  # noqa: E402
    _recorder = _Recorder(Path(os.environ.get(
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# Auth middleware + pages
from app.auth.middleware import auth_middleware  # noqa: E402
from app.auth.pages import pages_router  # noqa: E402

app.middleware("http")(auth_middleware)
app.include_router(pages_router)

# API routers (Task 12)
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

# WebSocket
from app.ws.handler import router as _ws_router  # noqa: E402
app.include_router(_ws_router)


# ---------- static files ----------

STATIC_DIR = BRIDGE_ROOT / "static"
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
    return FileResponse(STATIC_DIR / "manifest.json",
                        media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _mount_uploads():
    udir = uploads_dir()
    app.mount("/uploads", StaticFiles(directory=str(udir)), name="uploads")


_mount_uploads()
