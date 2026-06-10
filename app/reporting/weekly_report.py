"""Weekly report post-hook.

`_weekly_report_posted(sid, label)` runs inside `report.scheduler_loop` after
a new weekly-report session is materialized. It tells live clients to
refresh the session list and fires a push notification so the user sees it
on their phone.
"""
from __future__ import annotations

import asyncio

import push

from app.log import get_logger
from app.ws.broadcast import broadcast

log = get_logger("bridge")


async def _weekly_report_posted(sid: str, label: str) -> None:
    """Hook called by report.scheduler_loop after a new weekly report session
    is created — tells connected clients to refresh + fires a push so the user
    sees it on their phone."""
    await broadcast({"type": "sessions_changed", "reason": "weekly_report",
                     "session_id": sid})
    try:
        await asyncio.to_thread(
            push.send_to_all,
            "📊 周报已生成",
            f"{label} · 打开 Phone Bridge 查看",
            None,
        )
    except Exception:
        log.exception("weekly report push failed")
