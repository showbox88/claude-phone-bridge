"""A single user-turn through the Claude SDK.

`run_user_turn(agent, text, images, files)` is what `/ws` handle_ws_message
calls for every incoming `user_message` frame. It:
1. Acquires `agent.turn_lock` (serialize one turn at a time for this session).
2. Auto-titles the session from the first message.
3. Persists the user message + builds the SDK content payload.
4. Streams the SDK response, broadcasting each block + persisting
   assistant_text / tool_use / tool_result rows.
5. On ResultMessage: records usage + cost, broadcasts turn_done.

`current_agent` ContextVar is set at the top of the turn so the SDK
permission callback (can_use_tool) can find the active agent without a
signature change at the SDK boundary.

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

from app.agent.agent import current_agent
from app.agent.content import _build_user_content
from app.agent.permission import truncate
from app.ws.broadcast import broadcast_to_agent

log = logging.getLogger("bridge")


def _save_msg(agent, role: str, content: dict) -> None:
    db.append_message(agent.session_id, role, content)


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
    agent, text: str, images: list[str] | None = None,
    files: list[str] | None = None,
) -> None:
    images = images or []
    files = files or []
    current_agent.set(agent)
    async with agent.turn_lock:
        if agent.client is None:
            await broadcast_to_agent(agent,
                {"type": "error", "msg": "no active session"})
            return
        # auto-title from first user message in this session
        sess = db.get_session(agent.session_id)
        if sess is not None and not sess["title"] and text:
            db.update_session(agent.session_id, title=text.strip()[:40])

        _save_msg(agent, "user",
                  {"text": text, "images": images, "files": files})
        content = _build_user_content(text, images, files)

        async def msg_stream():
            yield {"type": "user",
                   "message": {"role": "user", "content": content},
                   "parent_tool_use_id": None}

        try:
            await agent.client.query(msg_stream())
            async for msg in agent.client.receive_response():
                if isinstance(msg, (AssistantMessage, UserMessage)):
                    for block in getattr(msg, "content", []) or []:
                        ev = _block_to_event(block)
                        if ev is None:
                            continue
                        await broadcast_to_agent(agent, ev)
                        if ev["type"] == "assistant_text":
                            _save_msg(agent, "assistant_text", {"text": ev["text"]})
                        elif ev["type"] == "tool_use":
                            _save_msg(agent, "tool_use", {
                                "id": ev["id"], "tool": ev["tool"],
                                "input": ev["input"],
                            })
                        elif ev["type"] == "tool_result":
                            _save_msg(agent, "tool_result", {
                                "id": ev["id"], "ok": ev["ok"],
                                "content": ev["content"],
                            })
                elif isinstance(msg, ResultMessage):
                    sid_from_sdk = getattr(msg, "session_id", None)
                    if sid_from_sdk:
                        agent.sdk_session_id = sid_from_sdk
                        db.update_session(agent.session_id,
                                          sdk_session_id=sid_from_sdk)
                    cost = getattr(msg, "total_cost_usd", None) or 0.0
                    usage = getattr(msg, "usage", None) or {}
                    in_tok = int(usage.get("input_tokens") or 0)
                    out_tok = int(usage.get("output_tokens") or 0)
                    cache_read = int(usage.get("cache_read_input_tokens") or 0)
                    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
                    duration = int(getattr(msg, "duration_ms", 0) or 0)
                    nturns = int(getattr(msg, "num_turns", 0) or 0)
                    db.append_turn(
                        agent.session_id,
                        model=agent.model, mode=agent.mode,
                        duration_ms=duration, num_turns=nturns,
                        input_tokens=in_tok, output_tokens=out_tok,
                        cache_read_tokens=cache_read,
                        cache_create_tokens=cache_create,
                        cost_usd=float(cost),
                    )
                    await broadcast_to_agent(agent, {
                        "type": "turn_done",
                        "session_id": sid_from_sdk,
                        "cost_usd": cost,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "duration_ms": duration,
                    })
                    break
        except asyncio.CancelledError:
            await broadcast_to_agent(agent,
                {"type": "system", "msg": "turn cancelled"})
            raise
        except Exception as e:
            log.exception("turn failed")
            await broadcast_to_agent(agent,
                {"type": "error", "msg": f"{type(e).__name__}: {e}"})
