"""WebSocket fan-out helper.

`broadcast(msg)` sends a JSON-encoded message to every currently-connected
WebSocket in `state.websockets`. Dead sockets (send raises) are discarded
in place. Imported by every subsystem that needs to push to the UI.
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


async def broadcast_to_agent(agent, msg: dict) -> None:
    """Phase-3-Task-6 stub: forwards to broadcast() for now. Task 8
    replaces with WS-binding-aware routing."""
    await broadcast(msg)
