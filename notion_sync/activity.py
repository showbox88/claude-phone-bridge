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


def pending_action_exists(client, *, op: str, pb_id: str = "",
                          notion_id: str = "") -> bool:
    """True iff Sync Activity already has a Pending row for this
    pb_id/notion_id/op combination. Used to make enqueue idempotent.

    At least one of pb_id / notion_id should be non-empty.
    """
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    clauses = [
        {"property": "op",       "select": {"equals": op}},
        {"property": "decision", "select": {"equals": "Pending"}},
    ]
    if pb_id:
        clauses.append({"property": "pb_id",
                        "rich_text": {"equals": pb_id}})
    if notion_id:
        clauses.append({"property": "notion_id",
                        "rich_text": {"equals": notion_id}})
    rows = client.query_database(db_id, filter_={"and": clauses}, page_size=1)
    return len(rows) > 0


def frozen_pairs_for_collection(client, *, collection: str
                                ) -> tuple[set[str], set[str]]:
    """Return (frozen_pb_ids, frozen_notion_ids) for rows the runner
    must NOT touch because they have a Pending Conflict or Delete?
    decision waiting for the user.

    Freeze semantics: once a row is in Sync Activity with decision=Pending,
    the row's data on both sides is locked. The runner skips it on every
    subsequent run until the user picks a decision (PR3 applies decisions
    and clears the Pending state). Prevents data loss from subsequent
    edits cascading into NotionOnlyChange / PbOnlyChange before the user
    can decide.
    """
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    filt = {"and": [
        {"property": "collection", "select": {"equals": collection}},
        {"property": "decision",   "select": {"equals": "Pending"}},
        {"or": [
            {"property": "op", "select": {"equals": "Conflict"}},
            {"property": "op", "select": {"equals": "Delete?"}},
        ]},
    ]}
    rows = client.query_database(db_id, filter_=filt)
    frozen_pb: set[str] = set()
    frozen_notion: set[str] = set()
    for r in rows:
        p = r.get("properties", {})
        pid = "".join(rt.get("plain_text", "") for rt in p.get("pb_id", {}).get("rich_text", []))
        nid = "".join(rt.get("plain_text", "") for rt in p.get("notion_id", {}).get("rich_text", []))
        if pid:
            frozen_pb.add(pid)
        if nid:
            frozen_notion.add(nid)
    return frozen_pb, frozen_notion


def frozen_pairs_for_all(client, *, collections: list[str]
                         ) -> dict[str, tuple[set[str], set[str]]]:
    """One-shot group-by version of frozen_pairs_for_collection.

    Returns {collection: (frozen_pb_ids, frozen_notion_ids)} for every
    name in `collections`. Makes a single Notion query filtered by
    'collection IN [...]' AND decision=Pending AND op IN (Conflict,
    Delete?), then groups results in Python.

    Replaces N sequential per-collection queries with one. Same freeze
    semantics as the legacy single-collection version: rows with a
    Pending Conflict/Delete? decision are off-limits until the user
    picks a decision.

    Collections in the input list that have no frozen rows still appear
    in the result map with empty sets — callers can do a plain
    `result[collection]` without KeyError concerns.
    """
    result: dict[str, tuple[set[str], set[str]]] = {
        c: (set(), set()) for c in collections
    }
    if not collections:
        return result
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    filt = {"and": [
        {"or": [{"property": "collection", "select": {"equals": c}}
                for c in collections]},
        {"property": "decision", "select": {"equals": "Pending"}},
        {"or": [
            {"property": "op", "select": {"equals": "Conflict"}},
            {"property": "op", "select": {"equals": "Delete?"}},
        ]},
    ]}
    rows = client.query_database(db_id, filter_=filt)
    for r in rows:
        p = r.get("properties", {})
        c = (p.get("collection", {}).get("select") or {}).get("name", "")
        if c not in result:
            continue
        pid = "".join(rt.get("plain_text", "") for rt in p.get("pb_id", {}).get("rich_text", []))
        nid = "".join(rt.get("plain_text", "") for rt in p.get("notion_id", {}).get("rich_text", []))
        if pid:
            result[c][0].add(pid)
        if nid:
            result[c][1].add(nid)
    return result
