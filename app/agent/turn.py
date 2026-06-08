"""A single user-turn through the Claude SDK.

`run_user_turn(text, images, files)` is what `/ws` handle_ws_message
calls for every incoming `user_message` frame. It:
1. Acquires `state.turn_lock` (serialize one turn at a time).
2. Auto-titles the session from the first message.
3. Persists the user message + builds the SDK content payload.
4. Streams the SDK response, broadcasting each block + persisting
   assistant_text / tool_use / tool_result rows.
5. On ResultMessage: records usage + cost, broadcasts turn_done.

`_save_msg` and `_block_to_event` are helpers; they're exported because
the ws handler still uses _save_msg directly for user-side cancellation
bookkeeping.
"""
from __future__ import annotations

import asyncio
import logging

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)

import db

from app.agent.content import _build_user_content
from app.agent.permission import truncate
from app.state import state
from app.ws.broadcast import broadcast

log = logging.getLogger("bridge")


def _save_msg(role: str, content: dict) -> None:
    if state.session_id:
        db.append_message(state.session_id, role, content)


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
