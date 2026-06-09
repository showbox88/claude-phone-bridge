"""Process-level mutable singleton shared by every subsystem.

After Phase 3, the per-session state (client / cwd / mode / model /
turn_lock / current_turn_task / sdk_session_id / client_tz) lives on
ClaudeAgent (`app/agent/agent.py`). This module keeps only truly
process-global state.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket


@dataclass
class AppState:
    cwd_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    websockets: set["WebSocket"] = field(default_factory=set)
    # WS → session_id binding. Set on connect and on cmd:load_session.
    # Lets broadcast_to_agent fan out only to the right subscribers.
    ws_sessions: "dict[WebSocket, str]" = field(default_factory=dict)
    # cb_id → asyncio.Future of the user's allow/deny decision
    pending: "dict[str, asyncio.Future]" = field(default_factory=dict)
    # cb_id → {tool, input, session_id} so reconnecting clients can re-render
    pending_meta: "dict[str, dict]" = field(default_factory=dict)
    # YOLO: process-wide toggle. Not persisted.
    auto_approve: bool = False


state: AppState = AppState()
