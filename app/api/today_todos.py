"""Today's pending-todos endpoint + ack — drives the header bell."""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib as _hashlib
import logging
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.integrations.pb import PBError

from app.io_utils import read_json_safe, write_json_atomic
from app.paths import DATA_DIR
from app.settings import settings

log = logging.getLogger("bridge")
router = APIRouter()


class _PBError(Exception):
    """PocketBase query failed (network, auth, or HTTP error). Raised by
    `_pb_get_json` so callers can distinguish 'no data today' from 'we
    couldn't reach PB at all' instead of silently returning an empty list."""


def _today_ack_path() -> Path:
    return DATA_DIR / "today_ack.json"


def _pb_get_json(path: str) -> dict:
    """GET a PocketBase endpoint. Raises _PBError on persistent failure.

    Delegates to the unified PBClient (lazy singleton in server.py) which
    handles 401 → forced re-auth → one retry internally plus 5xx/429
    backoff.
    """
    if not settings.pocketbase_url:
        raise _PBError("PocketBase not configured")
    # Lazy import to avoid app.api.today_todos ↔ server import cycle.
    from server import _pb_client
    try:
        return _pb_client().request("GET", path, retry_on_401=True,
                                    timeout=10.0)
    except PBError as e:
        log.warning("PB GET %s failed: %s", path, e)
        raise _PBError(str(e)) from e


def _today_todos_query() -> list[dict]:
    today = _dt.date.today().isoformat()
    f = (f"status='Pending' && "
         f"(due_date='' || due_date<='{today} 23:59:59')")
    q = urllib.parse.quote(f, safe="")
    data = _pb_get_json(
        f"/api/collections/todos/records?filter={q}"
        f"&sort=due_date,-priority&perPage=200")
    out = []
    for r in data.get("items", []):
        out.append({
            "id": r.get("id", ""),
            "title": r.get("title", ""),
            "due_date": r.get("due_date", "") or "",
            "priority": r.get("priority", "") or "Normal",
        })
    return out


def _today_signature(items: list[dict]) -> str:
    today = _dt.date.today().isoformat()
    ids = ",".join(sorted(x["id"] for x in items))
    return _hashlib.sha1(f"{today}|{ids}".encode()).hexdigest()[:16]


def _load_today_ack() -> dict:
    return read_json_safe(_today_ack_path(), default={})


def _save_today_ack(d: dict) -> None:
    write_json_atomic(_today_ack_path(), d, indent=None)


@router.get("/api/today-todos")
async def api_today_todos():
    """Return today's pending todos for the header bell. Auth is enforced by
    the global middleware (path is not in `_PUBLIC_EXACT`).

    On PB unreachable: returns {"ok": false, "error": "pb_unreachable"} so the
    client can keep its last-known bell state instead of treating the outage
    as 'no todos today'."""
    try:
        items = await asyncio.to_thread(_today_todos_query)
    except _PBError as e:
        return {"ok": False, "error": "pb_unreachable", "detail": str(e)}
    sig = _today_signature(items)
    ack = await asyncio.to_thread(_load_today_ack)
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "signature": sig,
        "acked": ack.get("signature") == sig and len(items) > 0,
    }


@router.post("/api/today-todos/ack")
async def api_today_todos_ack(body: dict):
    sig = (body or {}).get("signature", "")
    if not sig:
        raise HTTPException(400, "missing signature")
    await asyncio.to_thread(_save_today_ack,
        {"signature": sig, "at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
    return {"ok": True}
