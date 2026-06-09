"""WebSocket fan-out helpers.

`broadcast(msg)` — to every WS (system-wide events like sessions_changed).
`broadcast_to_agent(agent, msg)` — only to WSs bound to that agent's
session (assistant_text, tool_use, permission_request, turn_done).
"""
from __future__ import annotations

import json

from app.state import state


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
        state.ws_sessions.pop(ws, None)


async def broadcast_to_agent(agent, msg: dict) -> None:
    """Fan-out only to WSs bound to agent.session_id. Drops the frame
    quietly if no WS is bound (e.g. server-driven turn with no client
    connected — db row still persisted via _save_msg)."""
    sid = agent.session_id
    targets = [ws for ws in list(state.websockets)
               if state.ws_sessions.get(ws) == sid]
    if not targets:
        return
    payload = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in targets:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.websockets.discard(ws)
        state.ws_sessions.pop(ws, None)
