#!/usr/bin/env python3
"""Notion → PocketBase one-shot migration for Smart Note.

Pulls every page from each of 12 known databases plus all standalone pages
under the Smart Note parent, transforms properties + body content, and writes
them to the local PocketBase. Cross-database relations resolve in a second
pass once every notion_id ↔ pb_id mapping is known.

Idempotent within a single run, NOT between runs — re-running will create
duplicates unless PB is wiped first.

Env vars (loaded from /home/dev/phone-bridge/.env when run on the VM):
    NOTION_TOKEN
    POCKETBASE_URL
    POCKETBASE_ADMIN_EMAIL
    POCKETBASE_ADMIN_PASSWORD

Usage:
    python3 migrate_notion.py                  # full migration
    python3 migrate_notion.py --dry-run        # transform but don't write to PB
    python3 migrate_notion.py --only contacts  # single collection
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Database registry. Order = import order (deps come before dependents).
# Relations list maps Notion property name → target PB collection name.
# ---------------------------------------------------------------------------
DBS: list[dict[str, Any]] = [
    {"pb": "locations",      "notion_id": "257c34c1-ac50-455d-9c8a-8d810de5c1e5", "relations": {}},
    {"pb": "contacts",       "notion_id": "e304a6c3-4771-4c69-9ffc-97a672a1ac0c", "relations": {}},
    {"pb": "ideas",          "notion_id": "ea05cc2d-90b5-4e8a-9de8-ee836bacd557",
     "relations": {"Related Ideas": "ideas"}},
    {"pb": "todos",          "notion_id": "5d4e3f93-cf13-4707-97c5-59b38940baac", "relations": {}},
    {"pb": "daily_briefing", "notion_id": "aaccd49c-0c92-4eb1-9c9f-a383eff45ffc", "relations": {}},
    {"pb": "claude_memos",   "notion_id": "5fb38778-8803-479d-913a-84d8e624efdd", "relations": {}},
    {"pb": "transactions",   "notion_id": "36a55cbb-56d6-4d4b-8b88-6dda8d91ba6b", "relations": {}},
    {"pb": "plans",          "notion_id": "c951c7a9-a8f5-4ffd-aea2-1244e437ae46",
     "relations": {"Related Ideas": "ideas"}},
    {"pb": "trips",          "notion_id": "df7ea062-7b18-4c4f-98f1-bfec8258c3db",
     "relations": {"Related Plan": "plans", "Companions": "contacts"}},
    {"pb": "days",           "notion_id": "13329dea-4f55-4fc8-8e64-6c1ff19353bb",
     "relations": {"Trip": "trips", "Location": "locations"}},
    {"pb": "foods",          "notion_id": "8cc91c47-5b33-4117-a608-3dc0fe589e7d",
     "relations": {"Location": "locations"}},
    {"pb": "journal",        "notion_id": "ccc3b239-682d-47a1-a20e-e33b3c8fae44",
     "relations": {"Related Trip": "trips", "Related Day": "days"}},
]

# Per-collection: Notion property name → PB field name.
PROP_MAP: dict[str, dict[str, str]] = {
    "locations": {
        "Name": "name", "Address": "address", "City": "city", "Phone": "phone",
        "Type": "type", "Rating": "rating", "Visited": "visited",
    },
    "contacts": {
        "Name": "name", "Company": "company", "City": "city", "Email": "email",
        "Phone": "phone", "Birthday": "birthday", "Last contact": "last_contact",
        "Relationship": "relationship", "Tags": "tags",
    },
    "ideas": {
        "Title": "title", "Category": "category", "Status": "status",
        "Tags": "tags", "Connection notes": "connection_notes",
        "Conversation count": "conversation_count",
        "Related Ideas": "related_ideas",
    },
    "todos": {
        "Title": "title", "Due date": "due_date", "Completed at": "completed_at",
        "Priority": "priority", "Status": "status", "Executor": "executor",
        "Executor Ref ID": "executor_ref_id", "Tags": "tags",
    },
    "daily_briefing": {
        "Title": "title", "Date": "date", "Type": "type", "Status": "status",
        "Items pending count": "items_pending_count",
        "Items completed today": "items_completed_today",
        "Family events flagged": "family_events_flagged",
    },
    "claude_memos": {
        "Title": "title", "Date": "date", "Category": "category",
        "Priority": "priority", "Status": "status",
    },
    "transactions": {
        "Description": "description", "Amount": "amount", "Date": "date",
        "Type": "type", "Category": "category", "Card": "card",
        "Confirmation": "confirmation", "Source": "source",
    },
    "plans": {
        "Title": "title", "Category": "category", "Status": "status",
        "Progress": "progress", "Target date": "target_date",
        "Last update": "last_update", "Related Ideas": "related_ideas",
    },
    "trips": {
        "Title": "title", "Origin": "origin", "Destination": "destination",
        "Budget": "budget", "Status": "status", "Type": "type",
        "Dates": "_dates",  # split into date_start/date_end manually
        "Related Plan": "related_plan", "Companions": "companions",
    },
    "days": {
        "Name": "name", "Date": "date", "Reserved": "reserved",
        "Check-in": "checkin", "Amount": "amount", "Currency": "currency",
        "Rate": "rate", "Activity type": "activity_type", "Score": "score",
        "Note": "note", "Trip": "trip", "Location": "location",
    },
    "foods": {
        "Dish": "dish", "Currency": "currency", "Price": "price",
        "Flavor": "flavor", "Rating": "rating", "Want again": "want_again",
        "Location": "location",
    },
    "journal": {
        "Title": "title", "Date": "date", "Mood": "mood", "Type": "type",
        "Tags": "tags", "Related Trip": "related_trip",
        "Related Day": "related_day",
    },
}

# Single-relation fields (PB maxSelect=1). Multi-relation goes as JSON array.
SINGLE_REL_FIELDS = {
    ("plans", "related_ideas"): False,    # multi
    ("trips", "related_plan"): True,
    ("trips", "companions"): False,       # multi
    ("days", "trip"): True,
    ("days", "location"): True,
    ("foods", "location"): True,
    ("journal", "related_trip"): True,
    ("journal", "related_day"): True,
    ("ideas", "related_ideas"): False,    # self, multi
}

NOTION_PARENT = "369acd0fbb8980c8ac72fdab06e709c4"   # Smart Note page id


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def http(method: str, url: str, *, headers: dict | None = None,
         body: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"message": raw[:500]}


def notion_get(path: str) -> dict:
    code, data = http("GET", f"https://api.notion.com{path}", headers={
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": "2022-06-28",
    })
    if code != 200:
        raise RuntimeError(f"notion GET {path}: {code} {data}")
    return data


def notion_post(path: str, body: dict) -> dict:
    code, data = http("POST", f"https://api.notion.com{path}", headers={
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }, body=body)
    if code != 200:
        raise RuntimeError(f"notion POST {path}: {code} {data}")
    return data


_pb_token: str | None = None

def pb_token() -> str:
    global _pb_token
    if _pb_token:
        return _pb_token
    url = os.environ["POCKETBASE_URL"].rstrip("/") + "/api/collections/_superusers/auth-with-password"
    code, data = http("POST", url, headers={"Content-Type": "application/json"}, body={
        "identity": os.environ["POCKETBASE_ADMIN_EMAIL"],
        "password":  os.environ["POCKETBASE_ADMIN_PASSWORD"],
    })
    if code != 200:
        raise RuntimeError(f"PB auth: {code} {data}")
    _pb_token = data["token"]
    return _pb_token


def pb(method: str, path: str, body: dict | None = None) -> dict:
    url = os.environ["POCKETBASE_URL"].rstrip("/") + path
    code, data = http(method, url, headers={
        "Authorization": pb_token(),
        "Content-Type": "application/json",
    }, body=body)
    if code >= 400:
        raise RuntimeError(f"PB {method} {path}: {code} {data}")
    return data


# ---------------------------------------------------------------------------
# Notion blocks → markdown
# ---------------------------------------------------------------------------
def rich_text_to_md(rt: list[dict]) -> str:
    out = []
    for t in rt or []:
        s = t.get("plain_text", "")
        ann = t.get("annotations", {}) or {}
        if ann.get("code"):           s = f"`{s}`"
        if ann.get("bold"):           s = f"**{s}**"
        if ann.get("italic"):         s = f"*{s}*"
        if ann.get("strikethrough"):  s = f"~~{s}~~"
        href = t.get("href")
        if href:
            s = f"[{s}]({href})"
        out.append(s)
    return "".join(out)


def block_to_md(block: dict, depth: int) -> list[str]:
    t = block.get("type")
    payload = block.get(t, {}) if t else {}
    indent = "  " * depth
    rt = lambda: rich_text_to_md(payload.get("rich_text", []))
    lines: list[str] = []
    if t == "paragraph":
        lines.append(indent + rt())
    elif t in ("heading_1", "heading_2", "heading_3"):
        lines.append(f"{'#' * int(t[-1])} {rt()}")
    elif t == "bulleted_list_item":
        lines.append(f"{indent}- {rt()}")
    elif t == "numbered_list_item":
        lines.append(f"{indent}1. {rt()}")
    elif t == "to_do":
        check = "x" if payload.get("checked") else " "
        lines.append(f"{indent}- [{check}] {rt()}")
    elif t == "quote":
        lines.append(f"{indent}> {rt()}")
    elif t == "callout":
        icon = (payload.get("icon") or {}).get("emoji", "💡")
        lines.append(f"{indent}> {icon} {rt()}")
    elif t == "divider":
        lines.append("---")
    elif t == "code":
        lang = payload.get("language", "")
        lines.append(f"```{lang}")
        lines.append(rt())
        lines.append("```")
    elif t == "image":
        src = (payload.get("external") or payload.get("file") or {}).get("url", "")
        cap = rich_text_to_md(payload.get("caption", []))
        lines.append(f"![{cap}]({src})")
    elif t == "bookmark":
        url = payload.get("url", "")
        lines.append(f"[{url}]({url})")
    elif t == "child_page":
        lines.append(f"{indent}📄 *(child page: {payload.get('title','')})*")
    elif t == "child_database":
        lines.append(f"{indent}📊 *(embedded database: {payload.get('title','')})*")
    elif t == "equation":
        lines.append(f"$$ {payload.get('expression','')} $$")
    else:
        text = rt()
        if text:
            lines.append(f"{indent}{text}")
        else:
            lines.append(f"{indent}<!-- skipped block: {t} -->")
    return lines


def fetch_block_children_md(block_id: str, depth: int = 0) -> str:
    out: list[str] = []
    cursor: str | None = None
    while True:
        path = f"/v1/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        data = notion_get(path)
        for block in data.get("results", []):
            out.extend(block_to_md(block, depth))
            if block.get("has_children"):
                child = fetch_block_children_md(block["id"], depth + 1)
                if child:
                    out.append(child)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return "\n".join(s for s in out if s is not None)


# ---------------------------------------------------------------------------
# Notion property → PB value
# ---------------------------------------------------------------------------
def prop_value(prop: dict) -> Any:
    t = prop.get("type")
    if t == "title":          return rich_text_to_md(prop.get("title", []))
    if t == "rich_text":      return rich_text_to_md(prop.get("rich_text", []))
    if t == "number":         return prop.get("number")
    if t == "select":
        s = prop.get("select"); return s.get("name") if s else None
    if t == "multi_select":   return [s["name"] for s in prop.get("multi_select", [])]
    if t == "checkbox":       return bool(prop.get("checkbox"))
    if t == "date":           return prop.get("date")  # dict {start, end, ...}
    if t == "email":          return prop.get("email") or ""
    if t == "phone_number":   return prop.get("phone_number") or ""
    if t == "url":            return prop.get("url") or ""
    if t == "relation":       return [r["id"] for r in prop.get("relation", [])]
    if t == "formula":
        f = prop.get("formula", {}); return f.get(f.get("type"))
    if t == "created_time":      return prop.get("created_time")
    if t == "last_edited_time":  return prop.get("last_edited_time")
    return None


def transform(record: dict, db_cfg: dict) -> tuple[dict, dict]:
    """Return (pb_fields_without_relations, pending_relations_by_pb_field)."""
    pb_rec: dict[str, Any] = {}
    pending: dict[str, list[str]] = {}
    pmap = PROP_MAP[db_cfg["pb"]]
    for notion_name, pb_name in pmap.items():
        prop = record.get("properties", {}).get(notion_name)
        if not prop:
            continue
        val = prop_value(prop)
        if val is None or val == "" or val == []:
            continue

        # Date: dict with start/end
        if isinstance(val, dict) and "start" in val:
            if pb_name == "_dates":  # Trip.Dates → 2 fields
                if val.get("start"): pb_rec["date_start"] = val["start"]
                if val.get("end"):   pb_rec["date_end"]   = val["end"]
                continue
            pb_rec[pb_name] = val["start"]
            continue

        if notion_name in db_cfg.get("relations", {}):
            pending[pb_name] = val
            continue

        pb_rec[pb_name] = val
    return pb_rec, pending


# ---------------------------------------------------------------------------
# Notion queries
# ---------------------------------------------------------------------------
def query_db(notion_db_id: str) -> Iterable[dict]:
    cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_post(f"/v1/databases/{notion_db_id}/query", body)
        for rec in data.get("results", []):
            yield rec
        if not data.get("has_more"):
            return
        cursor = data.get("next_cursor")
        time.sleep(0.4)  # be polite


def enumerate_standalone_pages(parent_id: str) -> list[dict]:
    """Walk the block tree under `parent_id`, returning every child_page block."""
    found: list[dict] = []

    def walk(block_id: str, parent_notion_id: str | None) -> None:
        cursor: str | None = None
        while True:
            path = f"/v1/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = notion_get(path)
            for b in data.get("results", []):
                if b.get("type") == "child_page":
                    found.append({
                        "id": b["id"],
                        "title": b.get("child_page", {}).get("title", ""),
                        "parent_notion_id": parent_notion_id,
                    })
                    walk(b["id"], b["id"])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    walk(parent_id, None)
    return found


# ---------------------------------------------------------------------------
# Migration steps
# ---------------------------------------------------------------------------
class State:
    """Tracks notion_id → pb_id, the collection name, and unresolved relations."""
    def __init__(self) -> None:
        self.pb_id: dict[str, str] = {}        # notion_id → PB id
        self.coll:  dict[str, str] = {}        # notion_id → PB collection name
        self.pending: dict[str, dict[str, list[str]]] = {}  # notion_id → {pb_field: [notion_id, ...]}

    def to_json(self) -> dict:
        return {"pb_id": self.pb_id, "coll": self.coll, "pending": self.pending}


def migrate_collection(db_cfg: dict, st: State, dry_run: bool) -> tuple[int, int]:
    coll = db_cfg["pb"]
    created = 0
    skipped = 0
    for rec in query_db(db_cfg["notion_id"]):
        pb_rec, pending = transform(rec, db_cfg)
        try:
            body_md = fetch_block_children_md(rec["id"]).strip()
            if body_md:
                pb_rec["content"] = body_md
        except Exception as e:  # noqa: BLE001
            print(f"    ! body for {rec['id'][:8]}: {e}", file=sys.stderr)

        title = pb_rec.get("title") or pb_rec.get("name") or pb_rec.get("dish") or pb_rec.get("description") or "(?)"

        if dry_run:
            print(f"    [dry] {coll:14} {rec['id'][:8]} {title[:50]}")
            st.pb_id[rec["id"]] = f"dryrun_{rec['id'][:12]}"
            st.coll[rec["id"]]  = coll
            if pending:
                st.pending[rec["id"]] = pending
            created += 1
            continue

        try:
            res = pb("POST", f"/api/collections/{coll}/records", pb_rec)
            st.pb_id[rec["id"]] = res["id"]
            st.coll[rec["id"]]  = coll
            if pending:
                st.pending[rec["id"]] = pending
            print(f"    + {coll:14} {res['id'][:8]} {title[:50]}")
            created += 1
        except Exception as e:  # noqa: BLE001
            print(f"    ! {coll} {rec['id'][:8]}: {e}", file=sys.stderr)
            skipped += 1
    return created, skipped


def resolve_relations(st: State, dry_run: bool) -> int:
    updated = 0
    for notion_id, rel_fields in st.pending.items():
        pb_id = st.pb_id.get(notion_id)
        coll = st.coll.get(notion_id)
        if not pb_id or not coll:
            continue
        patch: dict[str, Any] = {}
        for pb_field, notion_targets in rel_fields.items():
            resolved = [st.pb_id[nid] for nid in notion_targets if nid in st.pb_id]
            if not resolved:
                continue
            single = SINGLE_REL_FIELDS.get((coll, pb_field), False)
            patch[pb_field] = resolved[0] if single else resolved
        if not patch:
            continue
        if dry_run:
            print(f"    [dry] PATCH {coll}/{pb_id[:8]} {patch}")
        else:
            try:
                pb("PATCH", f"/api/collections/{coll}/records/{pb_id}", patch)
                print(f"    ~ {coll}/{pb_id[:8]} relations resolved")
            except Exception as e:  # noqa: BLE001
                print(f"    ! PATCH {coll}/{pb_id[:8]}: {e}", file=sys.stderr)
                continue
        updated += 1
    return updated


def migrate_pages(st: State, dry_run: bool) -> tuple[int, int]:
    pages = enumerate_standalone_pages(NOTION_PARENT)
    print(f"  found {len(pages)} standalone pages under Smart Note")
    created = 0
    skipped = 0
    pending_parents: dict[str, str] = {}

    for p in pages:
        try:
            page = notion_get(f"/v1/pages/{p['id']}")
        except Exception as e:  # noqa: BLE001
            print(f"    ! fetch page {p['id'][:8]}: {e}", file=sys.stderr)
            skipped += 1
            continue
        title = p["title"] or "(untitled)"
        icon_obj = page.get("icon") or {}
        icon = icon_obj.get("emoji", "") if icon_obj.get("type") == "emoji" else ""
        try:
            body_md = fetch_block_children_md(p["id"]).strip()
        except Exception as e:  # noqa: BLE001
            print(f"    ! body for page {p['id'][:8]}: {e}", file=sys.stderr)
            body_md = ""
        rec = {
            "title": title,
            "icon": icon,
            "content": body_md,
            "notion_id": p["id"],
            "notion_url": page.get("url", ""),
            "archived": bool(page.get("archived")),
        }
        if dry_run:
            print(f"    [dry] pages          {p['id'][:8]} {title[:50]}")
            st.pb_id[p["id"]] = f"dryrun_{p['id'][:12]}"
            st.coll[p["id"]]  = "pages"
            created += 1
        else:
            try:
                res = pb("POST", "/api/collections/pages/records", rec)
                st.pb_id[p["id"]] = res["id"]
                st.coll[p["id"]]  = "pages"
                print(f"    + pages          {res['id'][:8]} {title[:50]}")
                created += 1
            except Exception as e:  # noqa: BLE001
                print(f"    ! pages {p['id'][:8]}: {e}", file=sys.stderr)
                skipped += 1
                continue
        if p["parent_notion_id"]:
            pending_parents[p["id"]] = p["parent_notion_id"]

    # Second pass: parent relation
    for notion_id, parent_nid in pending_parents.items():
        pb_id = st.pb_id.get(notion_id)
        parent_pb_id = st.pb_id.get(parent_nid)
        if not pb_id or not parent_pb_id:
            continue
        if dry_run:
            print(f"    [dry] PATCH pages/{pb_id[:8]} parent={parent_pb_id[:8]}")
        else:
            try:
                pb("PATCH", f"/api/collections/pages/records/{pb_id}",
                   {"parent": parent_pb_id})
            except Exception as e:  # noqa: BLE001
                print(f"    ! page parent {pb_id[:8]}: {e}", file=sys.stderr)
    return created, skipped


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="run only this PB collection (e.g. trips)")
    ap.add_argument("--env", default="/home/dev/phone-bridge/.env")
    ap.add_argument("--id-map", default="/tmp/migration_id_map.json")
    ap.add_argument("--skip-pages", action="store_true")
    args = ap.parse_args()

    load_env(Path(args.env))
    for k in ("NOTION_TOKEN", "POCKETBASE_URL", "POCKETBASE_ADMIN_EMAIL", "POCKETBASE_ADMIN_PASSWORD"):
        if not os.environ.get(k):
            print(f"missing env: {k}", file=sys.stderr)
            return 1

    st = State()
    print("\n=== Pass 1: insert without relations ===")
    for db in DBS:
        if args.only and db["pb"] != args.only:
            continue
        print(f"\n→ {db['pb']}")
        c, s = migrate_collection(db, st, args.dry_run)
        print(f"  created={c} skipped={s}")

    if not args.only and not args.skip_pages:
        print("\n→ pages (standalone under Smart Note)")
        c, s = migrate_pages(st, args.dry_run)
        print(f"  created={c} skipped={s}")

    print("\n=== Pass 2: resolve relations ===")
    n = resolve_relations(st, args.dry_run)
    print(f"  updated={n}")

    Path(args.id_map).write_text(json.dumps(st.to_json(), indent=2, ensure_ascii=False))
    print(f"\nid map → {args.id_map}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
