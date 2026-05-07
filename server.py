"""FastAPI bridge: phone web UI <-> local Claude Code session.

Sessions persist in SQLite; each bridge session maps to an SDK session_id so
"continue history" works across restarts via SDK's `resume` option. Images
upload to .bridge_uploads/<session_id>/ and ride along as multimodal content.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import socket

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

import db
import push

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# Tools that auto-approve in CODE mode. Everything else hits the permission callback.
AUTO_ALLOW = {
    "Read", "Glob", "Grep",
    "WebFetch", "WebSearch",
    "TodoWrite", "NotebookRead", "BashOutput",
}
# In CHAT mode we just allow web browsing; nothing touches the filesystem.
CHAT_TOOLS = {"WebFetch", "WebSearch"}
CHAT_SYSTEM_PROMPT = (
    "You are Claude, a helpful AI assistant. The user is chatting casually. "
    "Be concise, friendly, and direct. You can use WebFetch/WebSearch when needed."
)

# Models the UI exposes. Empty string = use whatever Claude Code's default is.
AVAILABLE_MODELS = [
    {"id": "",       "label": "默认", "desc": "使用 Claude Code 默认配置"},
    {"id": "opus",   "label": "Opus", "desc": "最强推理 / 最贵"},
    {"id": "sonnet", "label": "Sonnet", "desc": "均衡 / 性价比"},
    {"id": "haiku",  "label": "Haiku", "desc": "快 / 便宜"},
]
AVAILABLE_MODES = [
    {"id": "code", "label": "代码", "desc": "Claude Code 完整工具链"},
    {"id": "chat", "label": "聊天", "desc": "纯对话，仅允许联网搜索"},
]

UPLOAD_DIRNAME = ".bridge_uploads"
MAX_IMAGES_PER_MESSAGE = 4
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB per file
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
ALLOWED_DOC_MIMES = {"application/pdf"}
# Extensions for text-like attachments — read as UTF-8 and embedded inline.
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".csv", ".tsv",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".env",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".scss",
    ".cpp", ".cc", ".c", ".h", ".hpp", ".java", ".kt", ".go", ".rs",
    ".rb", ".php", ".sh", ".bat", ".ps1", ".sql",
}
SHEET_EXTS = {".xlsx", ".xls"}
MAX_TEXT_INLINE_CHARS = 50_000   # cap per file when inlining text content
MAX_SHEET_ROWS_PER_SHEET = 200   # cap rows when converting xlsx to CSV-like text


@dataclass
class AppState:
    client: ClaudeSDKClient | None = None
    cwd_root: Path = field(
        default_factory=lambda: Path(os.environ.get("DEFAULT_CWD") or os.getcwd()).resolve()
    )
    cwd: Path = field(init=False)
    websockets: set[WebSocket] = field(default_factory=set)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    # cb_id -> {tool, input}: keeps the metadata so newly-connected clients
    # (e.g. the phone PWA after tapping a push notification) can re-render
    # the permission card on reconnect.
    pending_meta: dict[str, dict] = field(default_factory=dict)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_turn_task: asyncio.Task | None = None
    session_id: str | None = None        # bridge session id (uuid hex)
    sdk_session_id: str | None = None    # SDK's session_id, captured per turn
    mode: str = "code"                   # 'code' | 'chat'
    model: str = ""                      # model alias or "" for default

    def __post_init__(self):
        self.cwd = self.cwd_root


state = AppState()


def uploads_dir() -> Path:
    p = state.cwd_root / UPLOAD_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_in_root(rel: str) -> Path | None:
    """Return absolute path inside cwd_root, or None if it would escape."""
    rel = (rel or "").strip().lstrip("/\\")
    if rel in (".", ""):
        return state.cwd_root
    try:
        target = (state.cwd_root / rel).resolve()
        target.relative_to(state.cwd_root)
        return target
    except (ValueError, OSError):
        return None


def _to_rel(p: Path) -> str:
    try:
        rel = p.resolve().relative_to(state.cwd_root)
        s = str(rel).replace("\\", "/")
        return "" if s == "." else s
    except ValueError:
        return ""


# ---------- helpers ----------

def truncate(s: str, n: int = 800) -> str:
    return s if len(s) <= n else s[:n] + f" … ({len(s) - n} more chars)"


def summarize_input(tool_input: dict | None) -> str:
    if not tool_input:
        return "(no input)"
    try:
        return truncate(json.dumps(tool_input, ensure_ascii=False))
    except (TypeError, ValueError):
        return truncate(str(tool_input))


async def broadcast(msg: dict) -> None:
    payload = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in list(state.websockets):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.websockets.discard(ws)


def _save_msg(role: str, content: dict) -> None:
    if state.session_id:
        db.append_message(state.session_id, role, content)


# ---------- permission callback ----------

async def can_use_tool(tool_name: str, tool_input: dict, context):  # noqa: ARG001
    if tool_name in AUTO_ALLOW:
        return PermissionResultAllow(behavior="allow", updated_input=None)

    cb_id = secrets.token_urlsafe(8)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    state.pending[cb_id] = fut
    state.pending_meta[cb_id] = {"tool": tool_name, "input": tool_input}

    await broadcast({
        "type": "permission_request",
        "id": cb_id,
        "tool": tool_name,
        "input": tool_input,
    })
    await asyncio.to_thread(
        push.send_to_all,
        f"🔧 Claude wants to run {tool_name}",
        summarize_input(tool_input)[:180],
        cb_id,
    )

    try:
        decision = await asyncio.wait_for(fut, timeout=600)
    except asyncio.TimeoutError:
        await broadcast({"type": "system", "msg": f"{tool_name} timed out, denied"})
        return PermissionResultDeny(behavior="deny", message="user did not respond in time")
    finally:
        state.pending.pop(cb_id, None)
        state.pending_meta.pop(cb_id, None)

    if decision == "allow":
        return PermissionResultAllow(behavior="allow", updated_input=None)
    return PermissionResultDeny(behavior="deny", message="user rejected via web UI")


# ---------- claude session lifecycle ----------

def make_options(resume_sdk_id: str | None = None) -> ClaudeAgentOptions:
    kwargs: dict[str, Any] = dict(
        cwd=str(state.cwd),
        can_use_tool=can_use_tool,
    )
    if state.mode == "chat":
        kwargs["system_prompt"] = CHAT_SYSTEM_PROMPT
        kwargs["allowed_tools"] = list(CHAT_TOOLS)
    else:  # code mode
        kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code"}
        kwargs["allowed_tools"] = list(AUTO_ALLOW)
    if state.model:
        kwargs["model"] = state.model
    if resume_sdk_id:
        kwargs["resume"] = resume_sdk_id
    return ClaudeAgentOptions(**kwargs)


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


def classify_upload(filename: str, mime: str) -> str:
    """Return 'image' | 'pdf' | 'text' | 'sheet' | '' based on filename + mime."""
    ext = Path(filename).suffix.lower()
    mime = (mime or "").lower()
    if mime in ALLOWED_IMAGE_MIMES or ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if mime in ALLOWED_DOC_MIMES or ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXTS or mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        return "text"
    if ext in SHEET_EXTS:
        return "sheet"
    return ""


def _read_text_safe(path: Path) -> str:
    """Read a text file with reasonable encoding fallbacks."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            txt = data.decode(enc)
            if len(txt) > MAX_TEXT_INLINE_CHARS:
                txt = txt[:MAX_TEXT_INLINE_CHARS] + f"\n…(truncated, {len(txt) - MAX_TEXT_INLINE_CHARS} more chars)"
            return txt
        except UnicodeDecodeError:
            continue
    return "(unreadable encoding)"


def _read_xlsx_as_text(path: Path) -> str:
    """Convert an .xlsx file into a CSV-like text snapshot. Requires openpyxl."""
    try:
        import openpyxl  # type: ignore
    except ImportError:
        return "(无法解析 .xlsx：服务器未安装 openpyxl，运行 `pip install openpyxl` 后重试)"
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as e:  # noqa: BLE001
        return f"(无法打开 xlsx: {e})"
    sections: list[str] = []
    for ws in wb.worksheets:
        rows: list[str] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_SHEET_ROWS_PER_SHEET:
                rows.append(f"… (truncated at {MAX_SHEET_ROWS_PER_SHEET} rows)")
                break
            rows.append(",".join("" if v is None else str(v).replace(",", "\\,") for v in row))
        sections.append(f"--- Sheet: {ws.title} ---\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(sections) if sections else "(empty workbook)"


def _build_user_content(text: str, images: list[str], files: list[str]) -> list[dict]:
    """Build Anthropic-style content blocks: text + image/document base64 entries.

    `images` is the list of uploaded attachment relative paths under .bridge_uploads/;
    each entry is dispatched to an image block (PNG/JPEG/WEBP/GIF) or a document
    block (PDF) based on its mime. `files` are absolute paths on disk that Claude
    will read via its Read tool (only useful in code mode).
    """
    text_parts = [text] if text else []
    if files:
        text_parts.append("\n附加文件（已在本机，请按需 Read）：")
        for f in files:
            text_parts.append(f"- {f}")

    udir = uploads_dir()
    inline_text_blobs: list[str] = []   # text/sheet content collected for the text block
    blocks: list[dict] = []             # image/document content blocks

    for rel in images[:MAX_IMAGES_PER_MESSAGE]:
        rel_norm = rel.replace("\\", "/").lstrip("/")
        try:
            abs_p = (udir / rel_norm).resolve()
            abs_p.relative_to(udir.resolve())
        except (ValueError, OSError):
            log.warning("rejecting upload path outside uploads dir: %s", rel)
            continue
        if not abs_p.is_file():
            log.warning("upload not found: %s", abs_p)
            continue
        kind = classify_upload(abs_p.name, mimetypes.guess_type(abs_p.name)[0] or "")
        mime = mimetypes.guess_type(abs_p.name)[0] or "application/octet-stream"
        if kind == "image":
            data = base64.standard_b64encode(abs_p.read_bytes()).decode("ascii")
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data},
            })
        elif kind == "pdf":
            data = base64.standard_b64encode(abs_p.read_bytes()).decode("ascii")
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            })
        elif kind == "text":
            body = _read_text_safe(abs_p)
            inline_text_blobs.append(f"\n--- 附件: {abs_p.name} ---\n```\n{body}\n```")
        elif kind == "sheet":
            body = _read_xlsx_as_text(abs_p)
            inline_text_blobs.append(f"\n--- 附件: {abs_p.name} ---\n```csv\n{body}\n```")
        else:
            log.warning("skipping unsupported file %s", abs_p)

    if inline_text_blobs:
        text_parts.extend(inline_text_blobs)
    full_text = "\n".join(text_parts).strip() or "(no text)"
    content: list[dict] = [{"type": "text", "text": full_text}]
    content.extend(blocks)
    return content


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
    push.init()
    db.init(state.cwd_root / ".bridge_data" / "bridge.db")
    uploads_dir()
    try:
        latest = db.latest_session_id()
        if latest:
            await open_session(latest)
        else:
            await new_session()
    except Exception as e:
        log.exception("initial Claude session failed: %s", e)
    yield
    if state.client is not None:
        with contextlib.suppress(Exception):
            await state.client.disconnect()


app = FastAPI(lifespan=lifespan)

# Allow cross-origin requests so a phone-side PWA loaded from any PC can talk
# to any other PC's bridge over Tailscale. The user's auth/security model is
# Tailscale tailnet itself (only your own devices can route to these hosts).
_origins_env = os.environ.get("ALLOWED_ORIGINS", "*")
_allowed_origins = ["*"] if _origins_env.strip() == "*" else [
    o.strip() for o in _origins_env.split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.get("/api/health")
async def api_health():
    """Lightweight probe. The phone-side app polls this on each saved source
    to render online/offline status dots."""
    return {
        "ok": True,
        "name": os.environ.get("BRIDGE_NAME") or socket.gethostname(),
        "cwd_root": str(state.cwd_root).replace("\\", "/"),
        "session_id": state.session_id,
        "mode": state.mode,
        "model": state.model or "",
    }


# ---------- REST: VAPID/push ----------

@app.get("/api/vapid-public-key")
async def get_vapid_key():
    return {"key": os.environ.get("VAPID_PUBLIC_KEY", "")}


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
async def api_sessions_list():
    return {
        "current": state.session_id,
        "sessions": db.list_sessions(),
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


@app.get("/api/meta")
async def api_meta():
    """Static metadata for the UI: available modes & models."""
    return {"modes": AVAILABLE_MODES, "models": AVAILABLE_MODELS}


@app.delete("/api/sessions/{sid}")
async def api_sessions_delete(sid: str):
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    db.delete_session(sid)
    # remove uploads dir for this session
    sdir = uploads_dir() / sid
    if sdir.is_dir():
        for p in sdir.iterdir():
            with contextlib.suppress(OSError):
                p.unlink()
        with contextlib.suppress(OSError):
            sdir.rmdir()
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
        # Preserve a sensible extension for later mime detection on disk.
        if kind == "image" and mime in ALLOWED_IMAGE_MIMES:
            ext = mimetypes.guess_extension(mime) or ext_in or ".bin"
            if ext == ".jpe":
                ext = ".jpg"
        else:
            ext = ext_in or ".bin"
        name = f"{uuid.uuid4().hex}{ext}"
        dest = sdir / name
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
    await ws.accept()
    state.websockets.add(ws)
    log.info("websocket connected (total=%d)", len(state.websockets))
    try:
        # send hello with current session snapshot
        hello: dict[str, Any] = {
            "type": "hello",
            "cwd": _to_rel(state.cwd),
            "session_id": state.session_id,
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
        log.info("websocket closed (remaining=%d)", len(state.websockets))


async def handle_ws_message(ws: WebSocket, msg: dict) -> None:
    t = msg.get("type")
    if t == "user_message":
        text = (msg.get("text") or "").strip()
        images = msg.get("images") or []
        files = msg.get("files") or []
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
                for p in sdir.iterdir():
                    with contextlib.suppress(OSError):
                        p.unlink()
                with contextlib.suppress(OSError):
                    sdir.rmdir()
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
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
