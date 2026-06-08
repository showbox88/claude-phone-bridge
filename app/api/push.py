"""Web push subscription endpoints."""
from __future__ import annotations

from fastapi import APIRouter

import push

from app.settings import settings

router = APIRouter()


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
