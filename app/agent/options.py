"""Claude Agent SDK options builder.

`make_options(agent)` produces the `ClaudeAgentOptions` passed to
`ClaudeSDKClient(options=...)` per session. It assembles:
- cwd from `agent.cwd`
- the permission callback (`can_use_tool`)
- mode-specific system prompt + allowed tools
- PocketBase MCP server registration (if env-configured)
- runtime timezone note (if `agent.client_tz` is set)
- model override / resume id

`PB_MCP_SERVER` is built once at module import; if init fails the service
keeps working with PB tools disabled.
"""
from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

import pb_tools

from app.agent.permission import AUTO_ALLOW, CHAT_TOOLS, can_use_tool

log = logging.getLogger("bridge")

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


def make_options(agent) -> ClaudeAgentOptions:
    """Build SDK options from a ClaudeAgent. Replaces the old
    `make_options(resume_sdk_id)` signature; reads cwd/mode/model/
    client_tz/sdk_session_id from the agent instead of global state."""
    kwargs: dict[str, Any] = dict(
        cwd=str(agent.cwd),
        can_use_tool=can_use_tool,
    )
    if agent.mode == "chat":
        kwargs["system_prompt"] = CHAT_SYSTEM_PROMPT
        kwargs["allowed_tools"] = list(CHAT_TOOLS)
    else:
        kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code"}
        kwargs["allowed_tools"] = list(AUTO_ALLOW)

    if PB_MCP_SERVER:
        kwargs["mcp_servers"] = {pb_tools.SERVER_NAME: PB_MCP_SERVER}
        kwargs["allowed_tools"] = kwargs["allowed_tools"] + pb_tools.SAFE_TOOL_NAMES
        if isinstance(kwargs["system_prompt"], str):
            kwargs["system_prompt"] = kwargs["system_prompt"] + "\n\n" + pb_tools.PROMPT_HINT
        else:
            kwargs["system_prompt"] = {**kwargs["system_prompt"],
                                       "append": pb_tools.PROMPT_HINT}

    if agent.client_tz:
        tz_note = (
            f"\n\n[runtime] Current user timezone: {agent.client_tz}. "
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

    if agent.model:
        kwargs["model"] = agent.model
    if agent.sdk_session_id:
        kwargs["resume"] = agent.sdk_session_id
    return ClaudeAgentOptions(**kwargs)
