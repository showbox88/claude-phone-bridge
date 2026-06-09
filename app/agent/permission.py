"""Permission callback + tool-name allowlists.

`can_use_tool` is the gate Claude SDK hits before every tool call. The logic:

1. Fast-path 打卡: localhost PocketBase curl from Bash auto-allows (already
   sandboxed to a non-destructive surface — no phone prompt).
2. Whitelist (AUTO_ALLOW): read-only tools auto-allow.
3. state.auto_approve YOLO: broadcast a system msg and allow everything else.
4. Otherwise: broadcast a `permission_request`, fire a push notification, and
   block on `state.pending[cb_id]` until the phone responds or 600s elapses.

CHAT_TOOLS is the lean tool set used when mode=chat (web search + Read + Bash
gated by the localhost-PB fast-path).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

import push

from app.agent.agent import current_agent
from app.settings import settings
from app.state import state
from app.ws.broadcast import broadcast, broadcast_to_agent

log = logging.getLogger("bridge")

# Tools that auto-approve in CODE mode. Everything else hits the permission callback.
AUTO_ALLOW = {
    "Read", "Glob", "Grep",
    "WebFetch", "WebSearch",
    "TodoWrite", "NotebookRead", "BashOutput",
}
# In CHAT mode: web browsing + Read (for CHECKIN.md etc.) + Bash (gated by
# can_use_tool fast-path to localhost PocketBase only — no destructive surface).
CHAT_TOOLS = {"WebFetch", "WebSearch", "Bash", "Read"}


def truncate(s: str, n: int = 800) -> str:
    return s if len(s) <= n else s[:n] + f" … ({len(s) - n} more chars)"


def summarize_input(tool_input: dict | None) -> str:
    if not tool_input:
        return "(no input)"
    try:
        return truncate(json.dumps(tool_input, ensure_ascii=False))
    except (TypeError, ValueError):
        return truncate(str(tool_input))


async def can_use_tool(tool_name: str, tool_input: dict, context):  # noqa: ARG001
    agent = current_agent.get()
    # Fast-path: 打卡 Bash curl to local PocketBase — no phone confirmation needed.
    # Match strictly on localhost:8090 / 127.0.0.1:8090 to keep blast radius tight.
    if tool_name == "Bash" and settings.pocketbase_url:
        cmd = str(tool_input.get("command", ""))
        if ("127.0.0.1:8090" in cmd or "localhost:8090" in cmd) and \
                ("curl " in cmd or "curl\n" in cmd):
            return PermissionResultAllow(behavior="allow", updated_input=None)
    if tool_name in AUTO_ALLOW:
        return PermissionResultAllow(behavior="allow", updated_input=None)

    # state.auto_approve stays GLOBAL (process-wide YOLO toggle). Still surface
    # the tool call in the chat as a system message so the user has audit
    # trail of what got auto-run while they were away.
    if state.auto_approve:
        if agent is not None:
            await broadcast_to_agent(agent,
                {"type": "system", "msg": f"🚀 auto-approved {tool_name}"})
        else:
            await broadcast({"type": "system",
                             "msg": f"🚀 auto-approved {tool_name}"})
        return PermissionResultAllow(behavior="allow", updated_input=None)

    cb_id = secrets.token_urlsafe(8)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    state.pending[cb_id] = fut
    state.pending_meta[cb_id] = {
        "tool": tool_name, "input": tool_input,
        "session_id": agent.session_id if agent else None,
    }

    perm_msg = {
        "type": "permission_request",
        "id": cb_id,
        "tool": tool_name,
        "input": tool_input,
        "session_id": agent.session_id if agent else None,
    }
    if agent is not None:
        await broadcast_to_agent(agent, perm_msg)
    else:
        await broadcast(perm_msg)

    await asyncio.to_thread(
        push.send_to_all,
        f"🔧 Claude wants to run {tool_name}",
        summarize_input(tool_input)[:180],
        cb_id,
    )

    try:
        decision = await asyncio.wait_for(fut, timeout=600)
    except asyncio.TimeoutError:
        if agent is not None:
            await broadcast_to_agent(agent,
                {"type": "system", "msg": f"{tool_name} timed out, denied"})
            await broadcast_to_agent(agent,
                {"type": "permission_resolved", "id": cb_id, "decision": "timeout"})
        else:
            await broadcast({"type": "system",
                             "msg": f"{tool_name} timed out, denied"})
            await broadcast({"type": "permission_resolved",
                             "id": cb_id, "decision": "timeout"})
        return PermissionResultDeny(behavior="deny",
                                    message="user did not respond in time")
    finally:
        state.pending.pop(cb_id, None)
        state.pending_meta.pop(cb_id, None)

    if decision == "allow":
        return PermissionResultAllow(behavior="allow", updated_input=None)
    return PermissionResultDeny(behavior="deny", message="user rejected via web UI")
