"""Web push subscription + send endpoints."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

import push

from app.settings import settings

router = APIRouter()


def _is_loopback(request: Request) -> bool:
    """True when the peer is the local machine (PB hook on same host)."""
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


@router.get("/api/vapid-public-key")
async def get_vapid_key():
    return {"key": settings.vapid_public_key}


@router.post("/api/subscribe")
async def subscribe(sub: dict):
    push.add_sub(sub)
    return {"ok": True}


@router.post("/api/unsubscribe")
async def unsubscribe(sub: dict):
    push.remove_sub(sub)
    return {"ok": True}


@router.post("/api/push/send")
async def send_push(payload: dict, request: Request):
    """Trigger a push to all subscribers. Loopback-only — PB hooks call this.

    Body: {"title": str, "body": str, "tag": str | null}
    """
    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail="loopback only")
    title = str(payload.get("title") or "")
    body = str(payload.get("body") or "")
    tag_raw = payload.get("tag")
    tag = str(tag_raw) if tag_raw is not None else None
    await asyncio.to_thread(push.send_to_all, title, body, tag)
    return {"ok": True}
