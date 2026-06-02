"""Helpers for writing rows to the Sync Activity Notion DB.

The DB itself is created once by scripts/setup_notion_sync_db.py and its
id is stored in env var NOTION_SYNC_ACTIVITY_DB_ID (also persisted to .env
on the VM by the bootstrap script).

Snapshots are JSON-stringified into rich_text so we can replay decisions
when the user picks Use Notion / Use PB / Delete both.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _rich(text: str) -> dict:
    if not text:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:200]}}]}


def _select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def _date(iso: str | None) -> dict:
    return {"date": {"start": iso} if iso else None}


def _url(href: str | None) -> dict:
    return {"url": href or None}


def write_auto_applied(client, *, collection: str, direction: str,
                       summary: str, pb_id: str, notion_id: str,
                       record_link: str | None = None) -> dict:
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    return client.create_page(db_id, {
        "title":        _title(f"{collection} · {direction} ({summary[:60]})"),
        "op":           _select("Auto-applied"),
        "direction":    _select(direction),
        "collection":   _select(collection),
        "record_link":  _url(record_link),
        "pb_id":        _rich(pb_id),
        "notion_id":    _rich(notion_id),
        "summary":      _rich(summary),
        "decision":     _select("N/A"),
        "detected_at":  _date(_now_iso()),
        "applied_at":   _date(_now_iso()),
    })


def write_conflict(client, *, collection: str, summary: str,
                   pb_id: str, notion_id: str,
                   pb_snapshot: dict, notion_snapshot: dict,
                   record_link: str | None = None) -> dict:
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    return client.create_page(db_id, {
        "title":           _title(f"{collection} · 冲突 ({summary[:60]})"),
        "op":              _select("Conflict"),
        "direction":       _select("None"),
        "collection":      _select(collection),
        "record_link":     _url(record_link),
        "pb_id":           _rich(pb_id),
        "notion_id":       _rich(notion_id),
        "summary":         _rich(summary),
        "pb_snapshot":     _rich(json.dumps(pb_snapshot, ensure_ascii=False)),
        "notion_snapshot": _rich(json.dumps(notion_snapshot, ensure_ascii=False)),
        "decision":        _select("Pending"),
        "detected_at":     _date(_now_iso()),
    })


def write_possible_duplicate(client, *, collection: str, summary: str,
                             pb_id: str, notion_id: str,
                             pb_snapshot: dict, notion_snapshot: dict,
                             score: float,
                             record_link: str | None = None) -> dict:
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    return client.create_page(db_id, {
        "title":           _title(f"{collection} · 可能重复 score={score:.2f}"),
        "op":              _select("Possible duplicate"),
        "direction":       _select("None"),
        "collection":      _select(collection),
        "record_link":     _url(record_link),
        "pb_id":           _rich(pb_id),
        "notion_id":       _rich(notion_id),
        "summary":         _rich(summary),
        "pb_snapshot":     _rich(json.dumps(pb_snapshot, ensure_ascii=False)),
        "notion_snapshot": _rich(json.dumps(notion_snapshot, ensure_ascii=False)),
        "decision":        _select("Pending"),
        "detected_at":     _date(_now_iso()),
    })


def write_delete_question(client, *, collection: str, summary: str,
                          pb_id: str, notion_id: str,
                          snapshot: dict) -> dict:
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    return client.create_page(db_id, {
        "title":           _title(f"{collection} · 删除? {summary[:60]}"),
        "op":              _select("Delete?"),
        "direction":       _select("None"),
        "collection":      _select(collection),
        "pb_id":           _rich(pb_id),
        "notion_id":       _rich(notion_id),
        "summary":         _rich(summary),
        "pb_snapshot":     _rich(json.dumps(snapshot, ensure_ascii=False)),
        "decision":        _select("Pending"),
        "detected_at":     _date(_now_iso()),
    })
