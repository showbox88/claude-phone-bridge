"""FastAPI bridge: phone web UI <-> local Claude Code session."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

import push

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# Tools that auto-approve. Everything else hits the permission callback.
AUTO_ALLOW = {
    "Read", "Glob", "Grep",
    "WebFetch", "WebSearch",
    "TodoWrite", "NotebookRead", "BashOutput",
}


@dataclass
class AppState:
    client: ClaudeSDKClient | None = None
    cwd_root: Path = field(
        default_factory=lambda: Path(os.environ.get("DEFAULT_CWD") or os.getcwd()).resolve()
    )
    cwd: Path = field(init=False)
    websockets: set[WebSocket] = field(default_factory=set)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_turn_task: asyncio.Task | None = None

    def __post_init__(self):
        self.cwd = self.cwd_root


state = AppState()


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


# ---------- permission callback ----------

async def can_use_tool(tool_name: str, tool_input: dict, context):  # noqa: ARG001
    if tool_name in AUTO_ALLOW:
        return PermissionResultAllow(behavior="allow", updated_input=None)

    cb_id = secrets.token_urlsafe(8)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    state.pending[cb_id] = fut

    await broadcast({
        "type": "permission_request",
        "id": cb_id,
        "tool": tool_name,
        "input": tool_input,
    })
    # Push runs sync in a thread to avoid blocking the event loop.
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

    if decision == "allow":
        return PermissionResultAllow(behavior="allow", updated_input=None)
    return PermissionResultDeny(behavior="deny", message="user rejected via web UI")


# ---------- claude session lifecycle ----------

def make_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(state.cwd),
        system_prompt={"type": "preset", "preset": "claude_code"},
        allowed_tools=list(AUTO_ALLOW),
        can_use_tool=can_use_tool,
    )


async def init_client() -> None:
    if state.client is not None:
        with contextlib.suppress(Exception):
            await state.client.disconnect()
        state.client = None

    log.info("starting Claude session in cwd=%s", state.cwd)
    state.client = ClaudeSDKClient(options=make_options())
    await state.client.connect()
    await broadcast({"type": "system", "msg": f"session ready · cwd={state.cwd}"})


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
    # ToolResultBlock comes wrapped in UserMessage; detect by attrs to avoid hard import.
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


async def run_user_turn(text: str) -> None:
    async with state.turn_lock:
        if state.client is None:
            await init_client()
        assert state.client is not None
        try:
            await state.client.query(text)
            async for msg in state.client.receive_response():
                if isinstance(msg, (AssistantMessage, UserMessage)):
                    for block in getattr(msg, "content", []) or []:
                        ev = _block_to_event(block)
                        if ev is not None:
                            await broadcast(ev)
                elif isinstance(msg, ResultMessage):
                    await broadcast({
                        "type": "turn_done",
                        "session_id": getattr(msg, "session_id", None),
                        "cost_usd": getattr(msg, "total_cost_usd", None),
                    })
                    break
        except asyncio.CancelledError:
            await broadcast({"type": "system", "msg": "turn cancelled"})
            raise
        except Exception as e:
            log.exception("turn failed")
            await broadcast({"type": "error", "msg": f"{type(e).__name__}: {e}"})


# ---------- FastAPI ----------

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    push.init()
    try:
        await init_client()
    except Exception as e:
        log.exception("initial Claude session failed: %s", e)
    yield
    if state.client is not None:
        with contextlib.suppress(Exception):
            await state.client.disconnect()


app = FastAPI(lifespan=lifespan)


# ---------- REST ----------

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


@app.get("/api/browse")
async def browse(path: str = ""):
    target = _resolve_in_root(path)
    if target is None or not target.is_dir():
        raise HTTPException(404, "not a directory or outside root")
    entries = []
    try:
        for e in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name.startswith("."):
                continue  # skip dotfiles to keep the picker clean
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            entries.append({"name": e.name, "is_dir": is_dir})
    except PermissionError:
        raise HTTPException(403, "permission denied")
    rel = _to_rel(target)
    parent = None
    if rel:  # not root → has a parent inside root
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
    # Re-validate after appending the new name (defense in depth)
    if _resolve_in_root(_to_rel(new_dir)) is None:
        raise HTTPException(400, "outside root")
    try:
        new_dir.mkdir(exist_ok=False)
    except FileExistsError:
        raise HTTPException(409, "already exists")
    except OSError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "path": _to_rel(new_dir)}


# ---------- WebSocket ----------

@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    state.websockets.add(ws)
    log.info("websocket connected (total=%d)", len(state.websockets))
    try:
        await ws.send_text(json.dumps({
            "type": "system",
            "msg": f"connected · cwd={state.cwd}",
        }))
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
        if not text:
            return
        await broadcast({"type": "user_echo", "text": text})
        state.current_turn_task = asyncio.create_task(run_user_turn(text))
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
    if name == "new":
        await init_client()
    elif name == "cwd":
        rel = msg.get("path", "")
        new_cwd = _resolve_in_root(rel)
        if new_cwd is None or not new_cwd.is_dir():
            await broadcast({"type": "error", "msg": f"invalid cwd (outside root or not a dir): {rel}"})
            return
        state.cwd = new_cwd
        await init_client()
    elif name == "cancel":
        task = state.current_turn_task
        if task and not task.done():
            task.cancel()
        else:
            await broadcast({"type": "system", "msg": "nothing to cancel"})


# ---------- static files ----------

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js")
async def service_worker():
    # Service workers must be served from root for full-site scope.
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- entry ----------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
