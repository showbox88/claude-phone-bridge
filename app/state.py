"""Process-level mutable singleton shared by every subsystem.

`state` is the one global. Routers, agent helpers, the WS handler, and the
lifespan all import it and mutate fields in place.

The default `cwd_root` here is intentionally just `Path.cwd().resolve()` — the
real value (from settings.default_cwd) is set by the FastAPI lifespan handler
BEFORE anything else uses it. This keeps app/state.py free of the
app.settings import at module-load time.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient
    from fastapi import WebSocket


@dataclass
class AppState:
    client: "ClaudeSDKClient | None" = None
    cwd_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    cwd: Path = field(init=False)
    websockets: set["WebSocket"] = field(default_factory=set)
    client_tz: str = ""
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    # cb_id -> {tool, input}: keeps the metadata so newly-connected clients
    # (e.g. the phone PWA after tapping a push notification) can re-render
    # the permission card on reconnect.
    pending_meta: dict[str, dict] = field(default_factory=dict)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_turn_task: "asyncio.Task | None" = None
    session_id: str | None = None        # bridge session id (uuid hex)
    sdk_session_id: str | None = None    # SDK's session_id, captured per turn
    mode: str = "code"                   # 'code' | 'chat'
    model: str = ""                      # model alias or "" for default
    # YOLO toggle: when on, can_use_tool auto-approves every tool call instead
    # of broadcasting permission_request. Deliberately NOT persisted — resets
    # to False on every service restart so it can't quietly stay on across
    # deploys.
    auto_approve: bool = False

    def __post_init__(self) -> None:
        self.cwd = self.cwd_root


state: AppState = AppState()
