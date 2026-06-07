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
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    FastAPI, File, Form, HTTPException, Request, Response, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import socket

import auth as auth_mod

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)
import db
import push
import report
import pb_tools

import notion_sync.config as sync_config_registry
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.provisioner import provision_notion_db

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


def _save_msg(role: str, content: dict) -> None:
    if state.session_id:
        db.append_message(state.session_id, role, content)


async def _weekly_report_posted(sid: str, label: str) -> None:
    """Hook called by report.scheduler_loop after a new weekly report session
    is created — tells connected clients to refresh + fires a push so the user
    sees it on their phone."""
    await broadcast({"type": "sessions_changed", "reason": "weekly_report",
                     "session_id": sid})
    try:
        await asyncio.to_thread(
            push.send_to_all,
            "📊 周报已生成",
            f"{label} · 打开 Phone Bridge 查看",
            None,
        )
    except Exception:
        log.exception("weekly report push failed")


# ---------- claude session lifecycle ----------

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


def _block_to_event(block) -> dict | None:
    if isinstance(block, TextBlock):
        return {"type": "assistant_text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": getattr(block, "id", None),
            "tool": block.name,
            "input": block.input,
        }
    if hasattr(block, "tool_use_id"):
        content = getattr(block, "content", "")
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
                else:
                    parts.append(str(c))
            content = "".join(parts)
        return {
            "type": "tool_result",
            "id": block.tool_use_id,
            "ok": not getattr(block, "is_error", False),
            "content": truncate(str(content), 800),
        }
    return None


from app.agent.content import _build_user_content, _read_text_safe, _read_xlsx_as_text  # noqa: E402,F401


async def run_user_turn(
    text: str, images: list[str] | None = None, files: list[str] | None = None
) -> None:
    images = images or []
    files = files or []
    async with state.turn_lock:
        if state.client is None or state.session_id is None:
            await broadcast({"type": "error", "msg": "no active session"})
            return
        # auto-title from first user message in this session
        sess = db.get_session(state.session_id)
        if sess is not None and not sess["title"] and text:
            db.update_session(state.session_id, title=text.strip()[:40])

        # persist user message (with attachment metadata) before sending
        _save_msg("user", {"text": text, "images": images, "files": files})

        # Build structured content (text + image blocks) and stream into SDK
        content = _build_user_content(text, images, files)

        async def msg_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
            }

        try:
            await state.client.query(msg_stream())
            async for msg in state.client.receive_response():
                if isinstance(msg, (AssistantMessage, UserMessage)):
                    for block in getattr(msg, "content", []) or []:
                        ev = _block_to_event(block)
                        if ev is None:
                            continue
                        await broadcast(ev)
                        # persist assistant/tool blocks
                        if ev["type"] == "assistant_text":
                            _save_msg("assistant_text", {"text": ev["text"]})
                        elif ev["type"] == "tool_use":
                            _save_msg("tool_use", {
                                "id": ev["id"], "tool": ev["tool"], "input": ev["input"],
                            })
                        elif ev["type"] == "tool_result":
                            _save_msg("tool_result", {
                                "id": ev["id"], "ok": ev["ok"], "content": ev["content"],
                            })
                elif isinstance(msg, ResultMessage):
                    sid = getattr(msg, "session_id", None)
                    if sid:
                        state.sdk_session_id = sid
                        if state.session_id:
                            db.update_session(state.session_id, sdk_session_id=sid)
                    cost = getattr(msg, "total_cost_usd", None) or 0.0
                    usage = getattr(msg, "usage", None) or {}
                    in_tok = int(usage.get("input_tokens") or 0)
                    out_tok = int(usage.get("output_tokens") or 0)
                    cache_read = int(usage.get("cache_read_input_tokens") or 0)
                    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
                    duration = int(getattr(msg, "duration_ms", 0) or 0)
                    nturns = int(getattr(msg, "num_turns", 0) or 0)
                    if state.session_id:
                        db.append_turn(
                            state.session_id,
                            model=state.model,
                            mode=state.mode,
                            duration_ms=duration,
                            num_turns=nturns,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            cache_read_tokens=cache_read,
                            cache_create_tokens=cache_create,
                            cost_usd=float(cost),
                        )
                    await broadcast({
                        "type": "turn_done",
                        "session_id": sid,
                        "cost_usd": cost,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "duration_ms": duration,
                    })
                    break
        except asyncio.CancelledError:
            await broadcast({"type": "system", "msg": "turn cancelled"})
            raise
        except Exception as e:
            log.exception("turn failed")
            await broadcast({"type": "error", "msg": f"{type(e).__name__}: {e}"})


# ---------- bridge session helpers ----------

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


@app.get("/api/health")
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
        "session_id": state.session_id,
        "mode": state.mode,
        "model": state.model or "",
    })
    return base


# ---------- REST: today's todos (drives the header bell) ----------

def _today_ack_path() -> Path:
    """Co-located with the server script so the path is stable regardless of
    DEFAULT_CWD or the user navigating to a different cwd at runtime."""
    return Path(__file__).parent / ".bridge_data" / "today_ack.json"


class _PBError(Exception):
    """PocketBase query failed (network, auth, or HTTP error). Raised by
    `_pb_get_json` so callers can distinguish 'no data today' from 'we
    couldn't reach PB at all' instead of silently returning an empty list."""


def _pb_get_json(path: str) -> dict:
    """GET a PocketBase endpoint. Raises _PBError on persistent failure.

    Phase 1 delegates to the unified PBClient, which handles 401 →
    forced re-auth → one retry internally, plus 5xx/429 backoff. We
    catch PBError and re-raise as _PBError to keep this function's
    one caller (_today_todos_query) unchanged.
    """
    if not POCKETBASE_URL:
        raise _PBError("PocketBase not configured")
    try:
        return _pb_client().request("GET", path, retry_on_401=True,
                                    timeout=10.0)
    except PBError as e:
        log.warning("PB GET %s failed: %s", path, e)
        raise _PBError(str(e)) from e


def _today_todos_query() -> list[dict]:
    today = _dt.date.today().isoformat()
    f = (f"status='Pending' && "
         f"(due_date='' || due_date<='{today} 23:59:59')")
    q = urllib.parse.quote(f, safe="")
    data = _pb_get_json(
        f"/api/collections/todos/records?filter={q}"
        f"&sort=due_date,-priority&perPage=200")
    out = []
    for r in data.get("items", []):
        out.append({
            "id": r.get("id", ""),
            "title": r.get("title", ""),
            "due_date": r.get("due_date", "") or "",
            "priority": r.get("priority", "") or "Normal",
        })
    return out


def _today_signature(items: list[dict]) -> str:
    today = _dt.date.today().isoformat()
    ids = ",".join(sorted(x["id"] for x in items))
    return _hashlib.sha1(f"{today}|{ids}".encode()).hexdigest()[:16]


def _load_today_ack() -> dict:
    from app.io_utils import read_json_safe
    return read_json_safe(_today_ack_path(), default={})


def _save_today_ack(d: dict) -> None:
    from app.io_utils import write_json_atomic
    write_json_atomic(_today_ack_path(), d, indent=None)


@app.get("/api/today-todos")
async def api_today_todos():
    """Return today's pending todos for the header bell. Auth is enforced by
    the global middleware (path is not in `_PUBLIC_EXACT`).

    On PB unreachable: returns {"ok": false, "error": "pb_unreachable"} so the
    client can keep its last-known bell state instead of treating the outage
    as 'no todos today'."""
    try:
        items = await asyncio.to_thread(_today_todos_query)
    except _PBError as e:
        return {"ok": False, "error": "pb_unreachable", "detail": str(e)}
    sig = _today_signature(items)
    ack = await asyncio.to_thread(_load_today_ack)
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "signature": sig,
        "acked": ack.get("signature") == sig and len(items) > 0,
    }


@app.post("/api/today-todos/ack")
async def api_today_todos_ack(body: dict):
    sig = (body or {}).get("signature", "")
    if not sig:
        raise HTTPException(400, "missing signature")
    await asyncio.to_thread(_save_today_ack,
        {"signature": sig, "at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
    return {"ok": True}


@app.get("/.well-known/oauth-protected-resource/mcp")
async def mcp_oauth_resource_metadata():
    """RFC 9728 OAuth protected-resource metadata for the mcp_pb sibling
    service. claude.ai's Custom Connector probes this during OAuth discovery
    before DCR. Phone-bridge owns root-path Funnel; mcp_pb is at /mcp."""
    return {
        "resource": "https://dashboard-server.tail4cfa2.ts.net/mcp",
        "authorization_servers": ["https://dashboard-server.tail4cfa2.ts.net/mcp"],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    }


@app.get("/.well-known/oauth-authorization-server/mcp")
async def mcp_oauth_authorization_server_metadata():
    """RFC 8414 authorization-server metadata for issuer https://host/mcp.
    Per RFC 8414, metadata for an issuer with path component lives at
    /.well-known/oauth-authorization-server/<issuer-path>, which falls on the
    root-path Funnel (phone-bridge), not on /mcp. Endpoints below are under
    /mcp/* so they reach mcp_pb."""
    base = "https://dashboard-server.tail4cfa2.ts.net/mcp"
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        "revocation_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    }


# ---------- REST: VAPID/push ----------

@app.get("/api/vapid-public-key")
async def get_vapid_key():
    return {"key": settings.vapid_public_key}


@app.post("/api/subscribe")
async def subscribe(sub: dict):
    push.add_sub(sub)
    return {"ok": True}


@app.post("/api/unsubscribe")
async def unsubscribe(sub: dict):
    push.remove_sub(sub)
    return {"ok": True}


# ---------- REST: workspace browsing ----------

@app.get("/api/browse")
async def browse(path: str = ""):
    target = _resolve_in_root(path)
    if target is None or not target.is_dir():
        raise HTTPException(404, "not a directory or outside root")
    entries = []
    try:
        for e in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name.startswith("."):
                continue
            try:
                is_dir = e.is_dir()
                size = e.stat().st_size if not is_dir else 0
            except OSError:
                continue
            entries.append({"name": e.name, "is_dir": is_dir, "size": size})
    except PermissionError:
        raise HTTPException(403, "permission denied")
    rel = _to_rel(target)
    parent = None
    if rel:
        parent = _to_rel(target.parent)
    return {
        "root": str(state.cwd_root).replace("\\", "/"),
        "path": rel,
        "abs": str(target).replace("\\", "/"),
        "parent": parent,
        "entries": entries,
        "current": _to_rel(state.cwd),
    }


@app.post("/api/mkdir")
async def mkdir(body: dict):
    name = (body.get("name", "") or "").strip()
    if not name or any(c in name for c in "/\\:*?\"<>|") or name in (".", "..") or len(name) > 100:
        raise HTTPException(400, "invalid folder name")
    base = _resolve_in_root(body.get("path", ""))
    if base is None or not base.is_dir():
        raise HTTPException(404, "base not a directory")
    new_dir = base / name
    if _resolve_in_root(_to_rel(new_dir)) is None:
        raise HTTPException(400, "outside root")
    try:
        new_dir.mkdir(exist_ok=False)
    except FileExistsError:
        raise HTTPException(409, "already exists")
    except OSError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "path": _to_rel(new_dir)}


# ---------- REST: sessions ----------

@app.get("/api/sessions")
async def api_sessions_list(q: str = ""):
    return {
        "current": state.session_id,
        "sessions": db.search_sessions(q) if q.strip() else db.list_sessions(),
        "query": q,
    }


@app.post("/api/sessions")
async def api_sessions_create(body: dict | None = None):
    body = body or {}
    sid = await new_session(
        cwd_rel=body.get("cwd"),
        mode=body.get("mode") or "code",
        model=body.get("model") or "",
    )
    return {"id": sid}


@app.get("/api/sessions/{sid}")
async def api_sessions_get(sid: str):
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    return sess


@app.patch("/api/sessions/{sid}")
async def api_sessions_patch(sid: str, body: dict):
    upd: dict[str, Any] = {}
    if "title" in body: upd["title"] = str(body["title"])[:80]
    if "mode" in body and body["mode"] in ("code", "chat"): upd["mode"] = body["mode"]
    if "model" in body: upd["model"] = str(body["model"])[:32]
    if not upd:
        raise HTTPException(400, "nothing to update")
    db.update_session(sid, **upd)
    return {"ok": True}


@app.get("/api/usage")
async def api_usage():
    return db.usage_summary()


@app.get("/api/settings/weekly-report")
async def api_settings_weekly_report_get():
    return report.load()


@app.put("/api/settings/weekly-report")
async def api_settings_weekly_report_put(body: dict):
    patch: dict[str, Any] = {}
    if "enabled" in body:
        patch["enabled"] = bool(body["enabled"])
    if "weekday" in body:
        try:
            wd = int(body["weekday"])
            if 1 <= wd <= 7:
                patch["weekday"] = wd
        except (TypeError, ValueError):
            raise HTTPException(400, "weekday must be 1..7")
    if "hour" in body:
        try:
            h = int(body["hour"])
            if 0 <= h <= 23:
                patch["hour"] = h
        except (TypeError, ValueError):
            raise HTTPException(400, "hour must be 0..23")
    if "minute" in body:
        try:
            m = int(body["minute"])
            if 0 <= m <= 59:
                patch["minute"] = m
        except (TypeError, ValueError):
            raise HTTPException(400, "minute must be 0..59")
    if "timezone" in body:
        patch["timezone"] = str(body["timezone"])[:64]
    return report.save(patch)


@app.post("/api/settings/weekly-report/run-now")
async def api_settings_weekly_report_run_now(body: dict | None = None):
    window = (body or {}).get("window") or "current"
    if window not in ("current", "previous"):
        window = "current"
    sid, label = await report.run_now(str(state.cwd_root), window=window)
    await _weekly_report_posted(sid, label)
    return {"session_id": sid, "label": label}


@app.get("/api/meta")
async def api_meta():
    """Static metadata for the UI: available modes & models."""
    return {"modes": AVAILABLE_MODES, "models": AVAILABLE_MODELS}


# ---------- REST: Notion sync (manual trigger + settings) ----------

def _sync_log_path() -> Path:
    return Path(__file__).parent / ".bridge_data" / "sync.log"


def _latest_run_end_summary() -> dict[str, Any]:
    """Return the most recent `run_end` JSON line from sync.log, or {}."""
    p = _sync_log_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return {}
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if evt.get("event") == "run_end":
            return {k: v for k, v in evt.items() if k != "event"}
    return {}


@app.post("/api/sync/now")
async def api_sync_now(body: dict | None = None):
    """Trigger notion_sync.runner --force-now. Returns the run_end summary."""
    body = body or {}
    only = (body.get("collection") or "").strip() or None
    cmd = [sys.executable, "-m", "notion_sync.runner", "--force-now"]
    if only:
        cmd += ["--only", only]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(Path(__file__).parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(504, "sync runner timed out after 10 min")
    except FileNotFoundError as e:
        raise HTTPException(500, f"runner not found: {e}")
    summary = _latest_run_end_summary()
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "summary": summary,
        "stderr": (stderr or b"").decode("utf-8", "replace")[-400:],
    }


def _pb_sync_global() -> dict[str, Any]:
    """Read the single sync_global row via PBClient. Returns {} if PB
    creds aren't configured or PB is unreachable."""
    try:
        from notion_sync.pb_api import PBClient
        rows = PBClient().list_records("sync_global", sort="")
        return rows[0] if rows else {}
    except Exception:
        return {}


@app.get("/api/settings/notion-sync")
async def api_settings_notion_sync_get():
    """Return current sync_global settings (timezone, hours, paused)."""
    row = await asyncio.to_thread(_pb_sync_global)
    return {
        "id": row.get("id", ""),
        "timezone":          row.get("timezone") or "America/New_York",
        "sync_hour_local":   row.get("sync_hour_local"),
        "sync_hour_local_2": row.get("sync_hour_local_2"),
        "paused":            bool(row.get("paused")),
        "last_run_at":       row.get("last_run_at") or "",
    }


@app.put("/api/settings/notion-sync")
async def api_settings_notion_sync_put(body: dict):
    """Patch sync_global. Accepts timezone (str), sync_hour_local (0..23 or null),
    sync_hour_local_2 (0..23 or null), paused (bool)."""
    patch: dict[str, Any] = {}
    if "timezone" in body:
        patch["timezone"] = str(body["timezone"])[:64]
    for key in ("sync_hour_local", "sync_hour_local_2"):
        if key in body:
            v = body[key]
            if v in (None, "", "null"):
                patch[key] = None
            else:
                try:
                    h = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be 0..23 or null")
                if not (0 <= h <= 23):
                    raise HTTPException(400, f"{key} must be 0..23")
                patch[key] = h
    if "paused" in body:
        patch["paused"] = bool(body["paused"])
    if not patch:
        raise HTTPException(400, "nothing to update")

    def _apply() -> dict[str, Any]:
        from notion_sync.pb_api import PBClient
        pb = PBClient()
        rows = pb.list_records("sync_global", sort="")
        if not rows:
            raise RuntimeError("sync_global has no rows")
        pb.update_record("sync_global", rows[0]["id"], patch)
        return pb.list_records("sync_global", sort="")[0]

    try:
        row = await asyncio.to_thread(_apply)
    except Exception as e:
        raise HTTPException(500, f"update failed: {e}")
    return {
        "id": row.get("id", ""),
        "timezone":          row.get("timezone"),
        "sync_hour_local":   row.get("sync_hour_local"),
        "sync_hour_local_2": row.get("sync_hour_local_2"),
        "paused":            bool(row.get("paused")),
        "last_run_at":       row.get("last_run_at") or "",
    }


# ---------- REST: sync_config registry (Task 7) ----------

_SYSTEM_PB_COLLECTIONS = {
    "sync_config", "sync_global",
    "_pb_users_auth_", "_superusers", "_mfas", "_otps",
    "_authOrigins", "_externalAuths",
}


def _pb_collection_field_names(pb: PBClient, name: str) -> set[str]:
    """Field names of one PB collection. Raises if not found."""
    raw = pb._http("GET", f"/api/collections/{name}")  # noqa: SLF001
    return {f["name"] for f in raw.get("fields", [])}


@app.get("/api/sync/targets")
async def api_sync_targets_list():
    """List configured sync targets + PB collections still available to enable."""
    def _do():
        pb = PBClient()
        targets = sync_config_registry.load_all(pb, fresh=True)
        configured = [
            {
                "id": t.id, "collection": t.collection,
                "notion_db_id": t.notion_db_id,
                "enabled": t.enabled, "auto_sync": t.auto_sync,
                "title_field": t.title_field, "date_field": t.date_field,
                "field_map_overrides": t.field_map_overrides,
                "last_synced_at": t.last_synced_at,
                "last_sync_summary": t.last_sync_summary,
            }
            for t in targets
        ]
        configured_names = {t.collection for t in targets}
        all_colls = pb.list_collections()
        available = []
        for c in all_colls:
            if c.get("type") != "base":
                continue
            name = c.get("name", "")
            if not name or name in _SYSTEM_PB_COLLECTIONS or name in configured_names:
                continue
            fields = []
            for f in c.get("fields", []):
                spec = {"name": f["name"], "type": f["type"]}
                if f.get("required"): spec["required"] = True
                if f["type"] == "select":
                    spec["values"] = f.get("values", [])
                    spec["maxSelect"] = f.get("maxSelect", 1)
                fields.append(spec)
            available.append({"collection": name, "fields": fields})
        return {"configured": configured, "available": available}
    return await asyncio.to_thread(_do)


@app.post("/api/sync/targets")
async def api_sync_targets_create(body: dict | None = None):
    """End-to-end: provision Notion DB + insert sync_config + spawn reconcile."""
    body = body or {}
    collection  = (body.get("collection")  or "").strip()
    title_field = (body.get("title_field") or "").strip()
    date_field  = (body.get("date_field")  or "").strip()
    auto_sync   = bool(body.get("auto_sync"))
    if not collection or not title_field:
        return JSONResponse({"error": "collection and title_field required"},
                             status_code=400)

    def _validate_and_provision():
        pb = PBClient()
        nc = NotionClient()
        fields = _pb_collection_field_names(pb, collection)
        if title_field not in fields:
            raise HTTPException(status_code=400,
                detail=f"title_field={title_field!r} not on {collection!r}")
        if date_field and date_field not in fields:
            raise HTTPException(status_code=400,
                detail=f"date_field={date_field!r} not on {collection!r}")
        existing = sync_config_registry.get(collection, pb, fresh=True)
        if existing is not None:
            raise HTTPException(status_code=409,
                detail=f"sync_config row for {collection!r} already exists")
        notion_db_id = provision_notion_db(
            pb=pb, nc=nc, collection=collection, title_field=title_field,
        )
        pb.create_record("sync_config", {
            "collection": collection, "notion_db_id": notion_db_id,
            "enabled": True, "auto_sync": auto_sync,
            "title_field": title_field, "date_field": date_field,
            "field_map_overrides": {},
        })
        sync_config_registry.invalidate()
        return notion_db_id

    try:
        notion_db_id = await asyncio.to_thread(_validate_and_provision)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    asyncio.create_task(_spawn_reconcile_initial(collection))
    return {"ok": True, "notion_db_id": notion_db_id, "reconcile_started": True}


async def _spawn_reconcile_initial(collection: str) -> None:
    import sys
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "scripts/reconcile_initial.py", "--only", collection,
            cwd=str(BRIDGE_ROOT),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=600)
    except Exception as e:
        log.warning("reconcile_initial spawn for %s failed: %s", collection, e)


@app.patch("/api/sync/targets/{collection}")
async def api_sync_targets_patch(collection: str, body: dict | None = None):
    body = body or {}
    allowed = {"enabled", "auto_sync", "title_field", "date_field",
                "field_map_overrides"}
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        return JSONResponse({"error": "no recognized keys"}, status_code=400)

    def _do():
        pb = PBClient()
        rows = pb.list_records("sync_config",
                                filter=f"collection='{collection}'", sort="")
        if not rows:
            raise HTTPException(status_code=404,
                detail=f"no sync_config for {collection!r}")
        row_id = rows[0]["id"]
        if "title_field" in patch or "date_field" in patch:
            fields = _pb_collection_field_names(pb, collection)
            tf = patch.get("title_field", rows[0].get("title_field"))
            df = patch.get("date_field",  rows[0].get("date_field"))
            if tf and tf not in fields:
                raise HTTPException(status_code=400,
                    detail=f"title_field={tf!r} not on {collection!r}")
            if df and df not in fields:
                raise HTTPException(status_code=400,
                    detail=f"date_field={df!r} not on {collection!r}")
        updated = pb.update_record("sync_config", row_id, patch)
        sync_config_registry.invalidate()
        return updated

    try:
        return await asyncio.to_thread(_do)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/sync/targets/{collection}")
async def api_sync_targets_delete(collection: str):
    def _do():
        pb = PBClient()
        rows = pb.list_records("sync_config",
                                filter=f"collection='{collection}'", sort="")
        if not rows:
            raise HTTPException(status_code=404,
                detail=f"no sync_config for {collection!r}")
        notion_db_id = rows[0].get("notion_db_id", "")
        pb.delete_record("sync_config", rows[0]["id"])
        sync_config_registry.invalidate()
        return {"ok": True, "notion_db_id": notion_db_id}
    try:
        return await asyncio.to_thread(_do)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/sync/registry/export-snapshot")
async def api_sync_registry_export_snapshot():
    """Run scripts/dump_sync_registry.py and return the output path."""
    out_path = "notion_sync/registry.snapshot.yaml"
    import sys
    cmd = [sys.executable,
            "scripts/dump_sync_registry.py", "--path", out_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(BRIDGE_ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return JSONResponse(
                {"ok": False, "error": stderr.decode("utf-8", "replace")[:500]},
                status_code=500,
            )
        return {"ok": True, "path": out_path}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------- REST: nearby POI (for check-in modal) ----------

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Approximate distance in metres between two lat/lng points."""
    import math
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def _overpass_query(lat: float, lng: float, radius_m: int) -> list[dict]:
    """Query OpenStreetMap Overpass for nearby named POIs.

    Uses `nwr` (nodes + ways + relations) since US/EU mapping often draws
    stores as building polygons (ways) rather than single nodes. `out center`
    gives us a centroid lat/lng for each way so we can compute distance.
    """
    import aiohttp
    # Note: each `nwr[...]` is a separate filter; the (... ;) groups them.
    q = (
        f"[out:json][timeout:8];"
        f"(nwr[\"amenity\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"shop\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"tourism\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"leisure\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"office\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"craft\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"healthcare\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"building\"=\"retail\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"building\"=\"commercial\"][\"name\"](around:{radius_m},{lat},{lng}););"
        f"out center 40;"
    )
    out: list[dict] = []
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": q},
                headers={"User-Agent": "PhoneBridge/0.1 (checkin POI lookup)"},
            ) as r:
                if r.status != 200:
                    log.warning("Overpass HTTP %d", r.status)
                    return []
                data = await r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Overpass query failed: %s", e)
        return []

    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:en") or tags.get("name:zh") or tags.get("brand")
        if not name:
            continue
        # Nodes carry lat/lon directly; ways/relations expose `center` via
        # `out center`. Fall back gracefully if shape is unexpected.
        p_lat = el.get("lat")
        p_lng = el.get("lon")
        if p_lat is None or p_lng is None:
            c = el.get("center") or {}
            p_lat = c.get("lat")
            p_lng = c.get("lon")
        if p_lat is None or p_lng is None:
            continue
        # Pick the most-specific category tag for display.
        kind = (tags.get("amenity") or tags.get("shop")
                or tags.get("tourism") or tags.get("leisure")
                or tags.get("office") or tags.get("craft")
                or tags.get("healthcare") or tags.get("building") or "")
        el_type = el.get("type", "node")
        out.append({
            "name": str(name)[:80],
            "lat": float(p_lat),
            "lng": float(p_lng),
            "distance_m": int(round(_haversine_m(lat, lng, float(p_lat), float(p_lng)))),
            "type": kind,
            "address": tags.get("addr:street", "") or tags.get("addr:full", ""),
            "city": tags.get("addr:city", ""),
            "osm_id": f"{el_type}/{el.get('id')}" if el.get("id") else "",
            "amap_poi_id": "",
            "fsq_id": "",
            "source": "osm",
        })
    return out


async def _foursquare_query(lat: float, lng: float, radius_m: int) -> list[dict]:
    """Query Foursquare Places API for nearby POIs. Requires FOURSQUARE_KEY env.

    Foursquare has good US/global commercial coverage where OSM is sparse.
    Endpoint: https://places-api.foursquare.com/places/search
    """
    key = settings.foursquare_key.strip()
    if not key:
        return []
    import aiohttp
    out: list[dict] = []
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        params = {
            "ll": f"{lat},{lng}",
            "radius": str(radius_m),
            "limit": "25",
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "X-Places-Api-Version": "2025-06-17",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(
                "https://places-api.foursquare.com/places/search",
                params=params, headers=headers,
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("Foursquare HTTP %d: %s", r.status, body[:200])
                    return []
                data = await r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Foursquare query failed: %s: %s", type(e).__name__, e)
        return []

    for p in data.get("results") or []:
        name = p.get("name")
        if not name:
            continue
        # Newer places-api.foursquare.com returns latitude/longitude at the
        # top level of each place; the legacy v3 schema nested them under
        # `geocodes.main.{latitude,longitude}`. Support both for safety.
        p_lat = p.get("latitude")
        p_lng = p.get("longitude")
        if p_lat is None or p_lng is None:
            geo = (p.get("geocodes") or {}).get("main") or {}
            p_lat = geo.get("latitude"); p_lng = geo.get("longitude")
        if p_lat is None or p_lng is None:
            continue
        cats = p.get("categories") or []
        kind = cats[0]["name"] if cats and cats[0].get("name") else ""
        loc = p.get("location") or {}
        out.append({
            "name": str(name)[:80],
            "lat": float(p_lat),
            "lng": float(p_lng),
            "distance_m": int(p.get("distance") or
                              round(_haversine_m(lat, lng, float(p_lat), float(p_lng)))),
            "type": kind,
            "address": loc.get("address", "") or loc.get("formatted_address", ""),
            "city": loc.get("locality", "") or loc.get("region", ""),
            "osm_id": "",
            "amap_poi_id": "",
            # Newer API: fsq_place_id; legacy: fsq_id.
            "fsq_id": p.get("fsq_place_id") or p.get("fsq_id") or "",
            "source": "fsq",
        })
    return out


async def _amap_query(lat: float, lng: float, radius_m: int) -> list[dict]:
    """Query 高德 Web API /place/around. Requires AMAP_KEY env."""
    key = settings.amap_key.strip()
    if not key:
        return []
    import aiohttp
    out: list[dict] = []
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        params = {
            "location": f"{lng},{lat}",  # NB: 高德 uses lng,lat order
            "radius": str(radius_m),
            "extensions": "base",
            "offset": "25",
            "page": "1",
            "key": key,
        }
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(
                "https://restapi.amap.com/v3/place/around",
                params=params,
            ) as r:
                if r.status != 200:
                    log.warning("Amap HTTP %d", r.status)
                    return []
                data = await r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Amap query failed: %s", e)
        return []

    if str(data.get("status")) != "1":
        log.warning("Amap error: %s", data.get("info"))
        return []
    for p in data.get("pois") or []:
        loc = (p.get("location") or "").split(",")
        if len(loc) != 2:
            continue
        try:
            p_lng = float(loc[0]); p_lat = float(loc[1])
        except ValueError:
            continue
        out.append({
            "name": str(p.get("name") or "")[:80],
            "lat": p_lat,
            "lng": p_lng,
            "distance_m": int(p.get("distance") or
                              round(_haversine_m(lat, lng, p_lat, p_lng))),
            "type": p.get("type", "").split(";")[0] if p.get("type") else "",
            "address": p.get("address") or "",
            "city": p.get("cityname") or "",
            "osm_id": "",
            "amap_poi_id": p.get("id") or "",
            "fsq_id": "",
            "source": "amap",
        })
    return out


def _merge_pois(lists: list[list[dict]]) -> list[dict]:
    """Combine multiple POI lists, dedup by (lowercased name, ~30m radius).
    When two sources describe the same place, fold their IDs together."""
    merged: list[dict] = []
    for src in lists:
        for p in src:
            collapsed = False
            for m in merged:
                if (m["name"].lower() == p["name"].lower()
                        and _haversine_m(m["lat"], m["lng"], p["lat"], p["lng"]) < 30):
                    for k in ("osm_id", "amap_poi_id", "fsq_id"):
                        if not m.get(k) and p.get(k):
                            m[k] = p[k]
                    if not m.get("address") and p.get("address"):
                        m["address"] = p["address"]
                    if not m.get("city") and p.get("city"):
                        m["city"] = p["city"]
                    if p["distance_m"] < m["distance_m"]:
                        m["distance_m"] = p["distance_m"]
                    collapsed = True
                    break
            if not collapsed:
                merged.append(dict(p))
    merged.sort(key=lambda x: x["distance_m"])
    return merged


@app.get("/api/poi/around")
async def api_poi_around(lat: float, lng: float, radius: int = 200):
    """Return nearby POIs from Foursquare (US/global commercial), 高德 (CN),
    and OSM Overpass (global fallback). Empty list on total failure — UI
    should let the user type a name manually in that case."""
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(400, "invalid lat/lng")
    radius = max(50, min(int(radius), 1000))
    fsq_task  = asyncio.create_task(_foursquare_query(lat, lng, radius))
    amap_task = asyncio.create_task(_amap_query(lat, lng, radius))
    osm_task  = asyncio.create_task(_overpass_query(lat, lng, radius))
    fsq_pois, amap_pois, osm_pois = await asyncio.gather(fsq_task, amap_task, osm_task)
    # Order matters: a source listed earlier "wins" naming/typing on tie.
    # Foursquare first → richest commercial names; 高德 catches Chinese POIs.
    merged = _merge_pois([fsq_pois, amap_pois, osm_pois])
    return {"pois": merged[:15], "lat": lat, "lng": lng, "radius_m": radius}


@app.delete("/api/sessions/{sid}")
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


# ---------- REST: image upload ----------

@app.post("/api/upload")
async def api_upload(
    session_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    if len(files) > MAX_IMAGES_PER_MESSAGE:
        raise HTTPException(400, f"too many files (max {MAX_IMAGES_PER_MESSAGE})")

    sdir = uploads_dir() / session_id
    sdir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    for f in files:
        original_name = f.filename or "upload.bin"
        ext_in = Path(original_name).suffix.lower()
        mime = (f.content_type or "").lower()
        kind = classify_upload(original_name, mime)
        if not kind:
            raise HTTPException(400, f"unsupported file type: {original_name}")
        # Sanitize the user's original filename; this becomes the on-disk
        # basename so Claude (and `ls`) see the real name.
        safe_name = _safe_filename(original_name)
        # If the sanitized name has no extension AND we have a higher-confidence
        # mime-derived one for images, append it. Other kinds keep whatever the
        # user provided (PDF/text/sheet extensions are already meaningful).
        if "." not in safe_name and kind == "image" and mime in ALLOWED_IMAGE_MIMES:
            guessed = mimetypes.guess_extension(mime) or ""
            if guessed == ".jpe":
                guessed = ".jpg"
            if guessed:
                safe_name = safe_name + guessed
        # Each file gets its own short-uuid subdir; eliminates name collisions
        # within a session and keeps cleanup as a single rmtree.
        uid = uuid.uuid4().hex[:8]
        sub = sdir / uid
        sub.mkdir(parents=True, exist_ok=True)
        dest = sub / safe_name
        name = f"{uid}/{safe_name}"  # used in the relative path below
        size = 0
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"file too large (>{MAX_UPLOAD_BYTES} bytes)")
                out.write(chunk)
        rel = f"{session_id}/{name}"
        saved.append({
            "path": rel,
            "url": f"/uploads/{rel}",
            "mime": mime or mimetypes.guess_type(original_name)[0] or "",
            "size": size,
            "name": original_name,
            "kind": kind,  # 'image' | 'pdf' | 'text' | 'sheet'
        })
    return {"files": saved}


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
