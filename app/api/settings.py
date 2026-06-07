"""Settings routes: weekly-report (load/save/run-now) + notion-sync globals."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

import report

from app.reporting.weekly_report import _weekly_report_posted
from app.state import state

router = APIRouter()


@router.get("/api/settings/weekly-report")
async def api_settings_weekly_report_get():
    return report.load()


@router.put("/api/settings/weekly-report")
async def api_settings_weekly_report_put(body: dict):
    patch: dict[str, Any] = {}
    if "enabled" in body:
        patch["enabled"] = bool(body["enabled"])
    if "weekday" in body:
        try:
            wd = int(body["weekday"])
            if 1 <= wd <= 7:
                patch["weekday"] = wd
        except (TypeError, ValueError):
            raise HTTPException(400, "weekday must be 1..7")
    if "hour" in body:
        try:
            h = int(body["hour"])
            if 0 <= h <= 23:
                patch["hour"] = h
        except (TypeError, ValueError):
            raise HTTPException(400, "hour must be 0..23")
    if "minute" in body:
        try:
            m = int(body["minute"])
            if 0 <= m <= 59:
                patch["minute"] = m
        except (TypeError, ValueError):
            raise HTTPException(400, "minute must be 0..59")
    if "timezone" in body:
        patch["timezone"] = str(body["timezone"])[:64]
    return report.save(patch)


@router.post("/api/settings/weekly-report/run-now")
async def api_settings_weekly_report_run_now(body: dict | None = None):
    window = (body or {}).get("window") or "current"
    if window not in ("current", "previous"):
        window = "current"
    sid, label = await report.run_now(str(state.cwd_root), window=window)
    await _weekly_report_posted(sid, label)
    return {"session_id": sid, "label": label}


def _pb_sync_global() -> dict[str, Any]:
    """Read the single sync_global row via PBClient. Returns {} if PB
    creds aren't configured or PB is unreachable."""
    try:
        from notion_sync.pb_api import PBClient
        rows = PBClient().list_records("sync_global", sort="")
        return rows[0] if rows else {}
    except Exception:
        return {}


@router.get("/api/settings/notion-sync")
async def api_settings_notion_sync_get():
    """Return current sync_global settings (timezone, hours, paused)."""
    row = await asyncio.to_thread(_pb_sync_global)
    return {
        "id": row.get("id", ""),
        "timezone":          row.get("timezone") or "America/New_York",
        "sync_hour_local":   row.get("sync_hour_local"),
        "sync_hour_local_2": row.get("sync_hour_local_2"),
        "paused":            bool(row.get("paused")),
        "last_run_at":       row.get("last_run_at") or "",
    }


@router.put("/api/settings/notion-sync")
async def api_settings_notion_sync_put(body: dict):
    """Patch sync_global. Accepts timezone (str), sync_hour_local (0..23 or null),
    sync_hour_local_2 (0..23 or null), paused (bool)."""
    patch: dict[str, Any] = {}
    if "timezone" in body:
        patch["timezone"] = str(body["timezone"])[:64]
    for key in ("sync_hour_local", "sync_hour_local_2"):
        if key in body:
            v = body[key]
            if v in (None, "", "null"):
                patch[key] = None
            else:
                try:
                    h = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be 0..23 or null")
                if not (0 <= h <= 23):
                    raise HTTPException(400, f"{key} must be 0..23")
                patch[key] = h
    if "paused" in body:
        patch["paused"] = bool(body["paused"])
    if not patch:
        raise HTTPException(400, "nothing to update")

    def _apply() -> dict[str, Any]:
        from notion_sync.pb_api import PBClient
        pb = PBClient()
        rows = pb.list_records("sync_global", sort="")
        if not rows:
            raise RuntimeError("sync_global has no rows")
        pb.update_record("sync_global", rows[0]["id"], patch)
        return pb.list_records("sync_global", sort="")[0]

    try:
        row = await asyncio.to_thread(_apply)
    except Exception as e:
        raise HTTPException(500, f"update failed: {e}")
    return {
        "id": row.get("id", ""),
        "timezone":          row.get("timezone"),
        "sync_hour_local":   row.get("sync_hour_local"),
        "sync_hour_local_2": row.get("sync_hour_local_2"),
        "paused":            bool(row.get("paused")),
        "last_run_at":       row.get("last_run_at") or "",
    }
