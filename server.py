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
import re
import secrets
import shutil
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    FastAPI, File, Form, HTTPException, Request, Response, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
)
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
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

import db
import push
import report
import pb_tools

import notion_sync.config as sync_config_registry
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.provisioner import provision_notion_db

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

POCKETBASE_URL = os.environ.get("POCKETBASE_URL", "").rstrip("/")
POCKETBASE_ADMIN_EMAIL = os.environ.get("POCKETBASE_ADMIN_EMAIL", "")
POCKETBASE_ADMIN_PASSWORD = os.environ.get("POCKETBASE_ADMIN_PASSWORD", "")


def _pb_refresh_token() -> bool:
    """Auth against PocketBase and set PB_TOKEN/PB_URL env vars for child Bash."""
    if not (POCKETBASE_URL and POCKETBASE_ADMIN_EMAIL and POCKETBASE_ADMIN_PASSWORD):
        return False
    try:
        req = urllib.request.Request(
            POCKETBASE_URL + "/api/collections/_superusers/auth-with-password",
            data=json.dumps({
                "identity": POCKETBASE_ADMIN_EMAIL,
                "password": POCKETBASE_ADMIN_PASSWORD,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            os.environ["PB_TOKEN"] = data["token"]
            os.environ["PB_URL"] = POCKETBASE_URL
            log.info("PB token refreshed (len=%d)", len(data["token"]))
            return True
    except (urllib.error.URLError, KeyError, ValueError, OSError) as e:
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


# In-process PocketBase MCP server (mcp__pb__*). Lets the SDK session read/write
# Smart Note data via real tools instead of hand-rolled Bash + curl. Built once
# at import; only registered into ClaudeAgentOptions when PB creds are present.
# Guarded so any init failure degrades to "PB tools off" rather than taking the
# whole service down — every pre-existing feature must keep working regardless.
PB_MCP_SERVER = None
try:
    if pb_tools.enabled():
        PB_MCP_SERVER = pb_tools.build_server()
        log.info("PocketBase MCP tools enabled: %s",
                 ", ".join(pb_tools.SAFE_TOOL_NAMES + pb_tools.GATED_TOOL_NAMES))
    else:
        log.info("PocketBase MCP tools disabled (POCKETBASE_* env not set)")
except Exception as e:
    PB_MCP_SERVER = None
    log.exception("PocketBase MCP tools failed to init, continuing without them: %s", e)


# Tools that auto-approve in CODE mode. Everything else hits the permission callback.
AUTO_ALLOW = {
    "Read", "Glob", "Grep",
    "WebFetch", "WebSearch",
    "TodoWrite", "NotebookRead", "BashOutput",
}
# In CHAT mode: web browsing + Read (for CHECKIN.md etc.) + Bash (gated by
# can_use_tool fast-path to localhost PocketBase only — no destructive surface).
CHAT_TOOLS = {"WebFetch", "WebSearch", "Bash", "Read"}
CHAT_SYSTEM_PROMPT = (
    "You are Claude, a helpful AI assistant. The user is chatting casually. "
    "Be concise, friendly, and direct. You can use WebFetch/WebSearch when needed. "
    "If the user message contains a ```checkin fenced code block, FIRST read "
    "/home/dev/phone-bridge/CHECKIN.md and follow its rules exactly to write "
    "the 打卡 data into the local PocketBase. Use curl with $PB_URL and "
    "$PB_TOKEN env vars (already set by the server). After writing, reply with "
    "a one-line confirmation."
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
    client_tz: str = ""
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
    # YOLO toggle: when on, can_use_tool auto-approves every tool call instead
    # of broadcasting permission_request. Deliberately NOT persisted — resets
    # to False on every service restart so it can't quietly stay on across
    # deploys.
    auto_approve: bool = False

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


# ---------- permission callback ----------

async def can_use_tool(tool_name: str, tool_input: dict, context):  # noqa: ARG001
    # Fast-path: 打卡 Bash curl to local PocketBase — no phone confirmation needed.
    # Match strictly on localhost:8090 / 127.0.0.1:8090 to keep blast radius tight.
    if tool_name == "Bash" and POCKETBASE_URL:
        cmd = str(tool_input.get("command", ""))
        if ("127.0.0.1:8090" in cmd or "localhost:8090" in cmd) and \
                ("curl " in cmd or "curl\n" in cmd):
            return PermissionResultAllow(behavior="allow", updated_input=None)
    if tool_name in AUTO_ALLOW:
        return PermissionResultAllow(behavior="allow", updated_input=None)

    # YOLO toggle: skip the phone prompt entirely. Still surface the
    # tool call in the chat as a system message so the user has audit
    # trail of what got auto-run while they were away.
    if state.auto_approve:
        await broadcast({
            "type": "system",
            "msg": f"🚀 auto-approved {tool_name}",
        })
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
        await broadcast({"type": "permission_resolved", "id": cb_id, "decision": "timeout"})
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

    # PocketBase tools: register the in-process MCP server and pre-approve the
    # read/safe-write tools (matching the old auto-allowed localhost curl path).
    # Destructive tools stay out of allowed_tools, so they hit can_use_tool and
    # the phone permission prompt.
    if PB_MCP_SERVER:
        kwargs["mcp_servers"] = {pb_tools.SERVER_NAME: PB_MCP_SERVER}
        kwargs["allowed_tools"] = kwargs["allowed_tools"] + pb_tools.SAFE_TOOL_NAMES
        if isinstance(kwargs["system_prompt"], str):
            kwargs["system_prompt"] = kwargs["system_prompt"] + "\n\n" + pb_tools.PROMPT_HINT
        else:
            kwargs["system_prompt"] = {**kwargs["system_prompt"],
                                       "append": pb_tools.PROMPT_HINT}

    if state.client_tz:
        tz_note = (
            f"\n\n[runtime] Current user timezone: {state.client_tz}. "
            f"When a user says relative times like '明天3点' or 'tomorrow 6pm', "
            f"resolve them per the rules in SMARTNOTE_PROMPT.md (Timezone section)."
        )
        sp = kwargs.get("system_prompt")
        if isinstance(sp, str):
            kwargs["system_prompt"] = sp + tz_note
        elif isinstance(sp, dict):
            kwargs["system_prompt"] = {
                **sp,
                "append": (sp.get("append", "") or "") + tz_note,
            }

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


def _safe_filename(name: str) -> str:
    """Strip filesystem-hostile bytes from an uploaded filename.

    Preserves spaces and Unicode (CJK, emoji); rejects only path separators,
    control bytes, leading dots, and over-long names. Falls back to
    'upload.bin' when nothing usable remains.
    """
    # basename only — drop any path components the client tried to sneak in
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # control chars (incl. null) — disallowed on all real filesystems
    name = re.sub(r"[\x00-\x1f]", "", name)
    # no leading dot (avoid hidden files / dotfile collisions)
    name = name.lstrip(".")
    # cap length to stay well under filesystem limits
    name = name[:200]
    return name or "upload.bin"


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
    advertised_paths: list[tuple[Path, str]] = []  # (abs_p, mime) for the trailing path block

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
            advertised_paths.append((abs_p, mime))
        elif kind == "pdf":
            data = base64.standard_b64encode(abs_p.read_bytes()).decode("ascii")
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            })
            advertised_paths.append((abs_p, mime))
        elif kind == "text":
            body = _read_text_safe(abs_p)
            inline_text_blobs.append(f"\n--- 附件: {abs_p.name} ---\n```\n{body}\n```")
            advertised_paths.append((abs_p, mime))
        elif kind == "sheet":
            body = _read_xlsx_as_text(abs_p)
            inline_text_blobs.append(f"\n--- 附件: {abs_p.name} ---\n```csv\n{body}\n```")
            advertised_paths.append((abs_p, mime))
        else:
            log.warning("skipping unsupported file %s", abs_p)

    if inline_text_blobs:
        text_parts.extend(inline_text_blobs)
    full_text = "\n".join(text_parts).strip() or "(no text)"
    content: list[dict] = [{"type": "text", "text": full_text}]
    content.extend(blocks)

    # Trailing "files on disk" block so Claude can Read / Bash on the originals.
    if advertised_paths:
        path_lines = []
        for abs_p, mime in advertised_paths:
            try:
                size_kb = max(1, abs_p.stat().st_size // 1024)
            except OSError:
                continue
            path_lines.append(f"- {abs_p} ({mime}, {size_kb} KB)")
        if path_lines:
            content.append({
                "type": "text",
                "text": (
                    "[Attached files on server disk — you can read, rename, move, "
                    "or upload them with Bash or Read tools:\n"
                    + "\n".join(path_lines)
                    + "]"
                ),
            })

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

_AUTH_FILE = Path(os.environ.get(
    "BRIDGE_AUTH_FILE", str(Path(__file__).resolve().parent / ".bridge_auth.json")
))
_COOKIE_DAYS = int(os.environ.get("BRIDGE_COOKIE_DAYS", "30"))
_COOKIE_SECONDS = _COOKIE_DAYS * 86400
auth_state = auth_mod.AuthState(_AUTH_FILE)

_PUBLIC_PREFIXES = ("/login", "/logout", "/setup", "/static/")
_PUBLIC_EXACT = {
    "/sw.js", "/manifest.json", "/icon.svg",
    "/api/health", "/api/vapid-public-key",
    # RFC 9728 OAuth protected-resource metadata for the mcp_pb sibling service.
    # Phone-bridge owns the root-path Tailscale Funnel mapping; mcp_pb's
    # public URL is /mcp on the same hostname. claude.ai's connector probes
    # this well-known URL during OAuth discovery before doing DCR.
    "/.well-known/oauth-protected-resource/mcp",
    # RFC 8414 path-suffixed authorization-server metadata. claude.ai's
    # connector tries this URL (not /mcp/.well-known/...) to find OAuth endpoints.
    "/.well-known/oauth-authorization-server/mcp",
}


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for p in _PUBLIC_PREFIXES:
        base = p.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


def _current_device(request: Request) -> dict | None:
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if not token:
        return None
    return auth_state.lookup_token(
        token,
        ip=auth_mod.client_ip(request),
        ua=request.headers.get("user-agent", ""),
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    # Not initialized yet → force first-time setup
    if not auth_state.is_initialized():
        if _wants_html(request):
            return RedirectResponse("/setup", status_code=303)
        return JSONResponse({"error": "not initialized"}, status_code=503)

    device = _current_device(request)
    if device is None:
        if _wants_html(request):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    request.state.device = device
    response = await call_next(request)
    # Sliding expiry: every authed request renews the cookie for another N days.
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if token:
        auth_mod.set_session_cookie(response, token, max_age=_COOKIE_SECONDS)
    return response


# ---------- Auth pages: shared HTML scaffold ----------

_AUTH_PAGE_CSS = """
:root{--bg:#0e1116;--card:#161b22;--line:#2a313a;--text:#e6edf3;--muted:#8b949e;
      --accent:#58a6ff;--red:#f85149;--green:#3fb950}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:1.6rem 1.4rem;width:100%;max-width:420px}
h1{margin:0 0 0.25rem;font-size:1.2rem}
.sub{color:var(--muted);font-size:0.85rem;margin-bottom:1.2rem}
label{display:block;color:var(--muted);font-size:0.78rem;text-transform:uppercase;
  letter-spacing:.05em;margin:0.85rem 0 0.3rem}
input[type=text],input[type=password]{width:100%;padding:0.65rem 0.75rem;
  background:#0b0f14;border:1px solid var(--line);border-radius:8px;color:var(--text);
  font:inherit;font-size:1rem}
input:focus{outline:none;border-color:var(--accent)}
button{width:100%;padding:0.7rem;margin-top:1.1rem;background:var(--accent);
  color:#0b0f14;border:0;border-radius:8px;font:inherit;font-weight:600;cursor:pointer;
  font-size:0.95rem}
button:hover{filter:brightness(1.07)}
.error{color:var(--red);font-size:0.85rem;margin-top:0.6rem;min-height:1.2em}
.muted{color:var(--muted);font-size:0.82rem}
.qr{display:flex;justify-content:center;margin:1rem 0;background:#ffffff;border-radius:8px;padding:0.75rem}
.qr svg{max-width:260px;height:auto;display:block}
.code{background:#0b0f14;border:1px solid var(--line);border-radius:6px;
  padding:0.6rem;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:0.85rem;
  word-break:break-all;color:#d2d2d2}
.devices li{list-style:none;padding:0.6rem 0;border-bottom:1px solid var(--line)}
.devices li:last-child{border:none}
.devices .row{display:flex;justify-content:space-between;align-items:center;gap:0.5rem}
.devices small{color:var(--muted);display:block;margin-top:0.15rem;font-size:0.75rem}
.devices form{margin:0}
.devices button.danger{padding:0.3rem 0.7rem;font-size:0.78rem;width:auto;
  background:transparent;border:1px solid var(--red);color:var(--red)}
.devices button.danger:hover{background:rgba(248,81,73,0.1)}
.this-device{color:var(--green);font-size:0.7rem;margin-left:0.4rem}
"""


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Phone Bridge</title>
<style>{_AUTH_PAGE_CSS}</style></head>
<body><div class="wrap"><div class="card">{body}</div></div></body></html>"""
    return HTMLResponse(html, status_code=status)


# ---------- /setup (first-time only) ----------

@app.get("/setup")
async def setup_get():
    if auth_state.is_initialized():
        # Stage 2 path — they may be mid-flow if no device exists yet
        if not auth_state.list_devices():
            return RedirectResponse("/setup/verify", status_code=303)
        return RedirectResponse("/login", status_code=303)
    return _page("First-time setup", """
<h1>First-time setup</h1>
<p class="sub">Set the master password. After this, scan the TOTP QR with your authenticator app.</p>
<form method="post" action="/setup">
  <label for="password">Password (min 12 chars)</label>
  <input id="password" name="password" type="password" minlength="12" required autofocus autocomplete="new-password">
  <label for="password2">Confirm</label>
  <input id="password2" name="password2" type="password" minlength="12" required autocomplete="new-password">
  <button type="submit">Continue</button>
</form>
""")


@app.post("/setup")
async def setup_post(request: Request, password: str = Form(...), password2: str = Form(...)):
    if auth_state.is_initialized():
        return RedirectResponse("/login", status_code=303)
    if password != password2:
        return _page("First-time setup", """<h1>First-time setup</h1>
<p class="error">Passwords don't match. <a href="/setup">Try again</a>.</p>""", status=400)
    if len(password) < 12:
        return _page("First-time setup", """<h1>First-time setup</h1>
<p class="error">Password too short (need at least 12). <a href="/setup">Try again</a>.</p>""", status=400)
    auth_state.initialize(password)
    return RedirectResponse("/setup/verify", status_code=303)


@app.get("/setup/verify")
async def setup_verify_get():
    if not auth_state.is_initialized() or auth_state.list_devices():
        return RedirectResponse("/login", status_code=303)
    secret = auth_state.totp_secret() or ""
    label = os.environ.get("BRIDGE_NAME") or socket.gethostname()
    uri = auth_mod.otpauth_uri(secret, label=label, issuer="Phone Bridge")
    qr = auth_mod.qr_svg(uri)
    # Pretty 4-char chunks for manual entry
    pretty_secret = " ".join(secret[i:i+4] for i in range(0, len(secret), 4))
    return _page("Scan TOTP", f"""
<h1>Add 2FA</h1>
<p class="sub">Three ways — pick whichever works:</p>

<p><b>1. On your phone:</b> tap this link, it'll open your Authenticator app and add the entry directly.</p>
<p style="margin:0.6rem 0 1.2rem"><a href="{uri}" style="display:inline-block;padding:0.6rem 1rem;background:#0b0f14;border:1px solid var(--accent);border-radius:8px;text-decoration:none">Open in Authenticator app →</a></p>

<p><b>2. Scan QR with Authenticator:</b></p>
<div class="qr">{qr}</div>

<p><b>3. Manual entry</b> (if scan fails) — in Google Authenticator: <i>+ → Enter a setup key</i></p>
<table style="width:100%;font-size:0.85rem;margin:0.5rem 0">
  <tr><td class="muted" style="padding:0.2rem 0;width:5em">Account</td><td><code>Phone Bridge</code></td></tr>
  <tr><td class="muted" style="padding:0.2rem 0">Key</td><td><code style="font-size:0.95rem">{pretty_secret}</code></td></tr>
  <tr><td class="muted" style="padding:0.2rem 0">Type</td><td>Time-based (TOTP)</td></tr>
</table>

<form method="post" action="/setup/verify" style="margin-top:1.5rem">
  <label for="code">After adding it, enter the current 6-digit code</label>
  <input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required autofocus autocomplete="one-time-code">
  <label for="device_name">This device's name</label>
  <input id="device_name" name="device_name" type="text" placeholder="e.g. Office PC" maxlength="40" value="">
  <button type="submit">Finish setup</button>
</form>
""")


@app.post("/setup/verify")
async def setup_verify_post(
    request: Request,
    code: str = Form(...),
    device_name: str = Form(""),
):
    if not auth_state.is_initialized() or auth_state.list_devices():
        return RedirectResponse("/login", status_code=303)
    if not auth_state.verify_totp(code):
        return _page("Scan TOTP", """<h1>Scan to add 2FA</h1>
<p class="error">Wrong code. <a href="/setup/verify">Try again</a>.</p>""", status=400)
    name = (device_name.strip() or _ua_short(request))[:40]
    token = auth_state.issue_device_token(
        name=name,
        ip=auth_mod.client_ip(request),
        ua=request.headers.get("user-agent", ""),
    )
    resp = RedirectResponse("/", status_code=303)
    auth_mod.set_session_cookie(resp, token, max_age=_COOKIE_SECONDS)
    return resp


# ---------- /login ----------

def _ua_short(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    if "iPhone" in ua: return "iPhone"
    if "iPad" in ua: return "iPad"
    if "Android" in ua: return "Android"
    if "Macintosh" in ua: return "Mac"
    if "Windows" in ua: return "Windows"
    if "Linux" in ua: return "Linux"
    return "device"


@app.get("/login")
async def login_get(request: Request):
    if not auth_state.is_initialized():
        return RedirectResponse("/setup", status_code=303)
    if _current_device(request):
        return RedirectResponse("/", status_code=303)
    return _page("Sign in", f"""
<h1>Sign in</h1>
<p class="sub">Phone Bridge — enter password and the 6-digit code from your authenticator.</p>
<form method="post" action="/login">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" required autofocus autocomplete="current-password">
  <label for="code">6-digit code</label>
  <input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required autocomplete="one-time-code">
  <label for="device_name">Name this device (optional)</label>
  <input id="device_name" name="device_name" type="text" maxlength="40" placeholder="e.g. {_ua_short(request)}">
  <button type="submit">Sign in</button>
</form>
""")


@app.post("/login")
async def login_post(
    request: Request,
    password: str = Form(...),
    code: str = Form(...),
    device_name: str = Form(""),
):
    if not auth_state.is_initialized():
        return RedirectResponse("/setup", status_code=303)
    ip = auth_mod.client_ip(request)
    allowed, retry_after = auth_state.can_attempt(ip)
    if not allowed:
        return _page("Sign in", f"""<h1>Sign in</h1>
<p class="error">Too many failed attempts. Try again in {retry_after}s.</p>""", status=429)
    if not (auth_state.verify_password(password) and auth_state.verify_totp(code)):
        auth_state.record_fail(ip)
        return _page("Sign in", """<h1>Sign in</h1>
<p class="error">Invalid password or code. <a href="/login">Try again</a>.</p>""", status=401)
    auth_state.clear_fails(ip)
    name = (device_name.strip() or _ua_short(request))[:40]
    token = auth_state.issue_device_token(
        name=name, ip=ip, ua=request.headers.get("user-agent", ""),
    )
    resp = RedirectResponse("/", status_code=303)
    auth_mod.set_session_cookie(resp, token, max_age=_COOKIE_SECONDS)
    return resp


# ---------- /logout ----------

@app.post("/logout")
@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if token:
        h = auth_mod._hash_token(token)
        auth_state.revoke(h)
    resp = RedirectResponse("/login", status_code=303)
    auth_mod.clear_session_cookie(resp)
    return resp


# ---------- /devices (manage logged-in devices) ----------

@app.get("/devices")
async def devices_get(request: Request):
    me = _current_device(request)  # already authed by middleware, but useful for "this device" marker
    devs = sorted(auth_state.list_devices(), key=lambda d: d.get("last_seen", 0), reverse=True)
    import datetime as _dt
    rows_html = []
    for d in devs:
        last = d.get("last_seen", 0)
        when = _dt.datetime.fromtimestamp(int(last)).strftime("%Y-%m-%d %H:%M") if last else "—"
        ip = d.get("last_ip", "") or "—"
        is_me = me and d["hash"] == me["hash"]
        marker = '<span class="this-device">THIS DEVICE</span>' if is_me else ""
        rows_html.append(f"""<li><div class="row">
  <div><b>{_html_escape(d.get('name','?'))}</b>{marker}<small>{ip} · last seen {when}</small></div>
  <form method="post" action="/devices/revoke">
    <input type="hidden" name="hash" value="{d['hash']}">
    <button class="danger" type="submit">Revoke</button>
  </form>
</div></li>""")
    body = f"""
<h1>Logged-in devices</h1>
<p class="sub">Revoke any device to log it out immediately.</p>
<ul class="devices">{''.join(rows_html) or '<li class="muted">No devices.</li>'}</ul>
<p style="margin-top:1.2rem"><a href="/">← back</a> · <a href="/logout">log out this device</a></p>
"""
    return _page("Devices", body)


@app.post("/devices/revoke")
async def devices_revoke(request: Request, hash: str = Form(...)):
    me = _current_device(request)
    auth_state.revoke(hash)
    if me and me["hash"] == hash:
        resp = RedirectResponse("/login", status_code=303)
        auth_mod.clear_session_cookie(resp)
        return resp
    return RedirectResponse("/devices", status_code=303)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
              .replace('"', "&quot;").replace("'", "&#39;"))


@app.get("/api/health")
async def api_health(request: Request):
    """Lightweight probe. Returns minimal info to anonymous clients (so the
    dashboard's HTTP probe still works without auth) and full session info
    only to authenticated devices."""
    base = {"ok": True}
    if auth_state.is_initialized() and _current_device(request) is None:
        return base
    base.update({
        "name": os.environ.get("BRIDGE_NAME") or socket.gethostname(),
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
    """GET a PocketBase admin endpoint with auto-retry on 401. Raises
    _PBError on persistent failure."""
    if not POCKETBASE_URL:
        raise _PBError("PocketBase not configured")
    token = os.environ.get("PB_TOKEN", "")
    url = POCKETBASE_URL + path
    last_err: Exception | None = None
    for attempt in (0, 1):
        req = urllib.request.Request(url,
            headers={"Authorization": token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 401 and attempt == 0 and _pb_refresh_token():
                token = os.environ.get("PB_TOKEN", "")
                continue
            break
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            break
    log.warning("PB GET %s failed: %s", path, last_err)
    raise _PBError(str(last_err))


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
    try:
        return json.loads(_today_ack_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_today_ack(d: dict) -> None:
    p = _today_ack_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d), encoding="utf-8")


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
        {"signature": sig, "at": _dt.datetime.now().isoformat()})
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
    try:
        proc = await asyncio.create_subprocess_exec(
            "/home/dev/phone-bridge/.venv/bin/python",
            "scripts/reconcile_initial.py", "--only", collection,
            cwd="/home/dev/phone-bridge",
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
    cmd = ["/home/dev/phone-bridge/.venv/bin/python",
            "scripts/dump_sync_registry.py", "--path", out_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd="/home/dev/phone-bridge",
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
    key = os.environ.get("FOURSQUARE_KEY", "").strip()
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
    key = os.environ.get("AMAP_KEY", "").strip()
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
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
