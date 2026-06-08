"""Per-session mutable state.

Each bridge session_id gets its own ClaudeAgent. Encapsulates everything
that used to live on the global `state` singleton but actually belongs to
a single session: the SDK client, working directory, mode/model, the
turn lock, the in-flight turn task, and the SDK's resume id.

The `current_agent` ContextVar lets the SDK permission callback
(can_use_tool, in app/agent/permission.py) find the agent that owns
the in-flight turn without changing the SDK boundary signature.
`run_user_turn` (app/agent/turn.py) sets it before invoking the SDK.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient


@dataclass
class ClaudeAgent:
    """All mutable state that used to live on `state` but actually belongs
    to a single bridge session. One instance per active session_id."""
    session_id: str                                   # bridge session id
    cwd: Path                                         # working directory
    mode: str = "code"                                # 'code' | 'chat'
    model: str = ""                                   # model alias or ""
    client_tz: str = ""                               # client-reported tz
    sdk_session_id: str | None = None                 # Claude SDK's session id (for resume)
    client: "ClaudeSDKClient | None" = None           # active SDK client
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_turn_task: "asyncio.Task | None" = None


# Set inside run_user_turn() before invoking the SDK. can_use_tool
# reads this when it needs to broadcast a permission_request scoped
# to the right session.
current_agent: ContextVar["ClaudeAgent | None"] = ContextVar(
    "current_agent", default=None
)
