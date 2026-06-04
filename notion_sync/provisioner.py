"""Auto-provision a Notion database to match a PB collection.

Used when the user enables sync for a previously-not-synced collection
from the settings UI. The created DB includes pb_id + last_synced_at
pipeline columns and the right Notion property type for every PB field.
"""
from __future__ import annotations

import os

from notion_sync.codec import snake_to_title
from notion_sync.config import load_all
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient


_SYSTEM_FIELD_NAMES = {
    "id", "created", "updated",
    "notion_id", "notion_last_edited", "last_synced_at", "pb_id",
}


_PIPELINE_FIELDS = [
    {"name": "notion_id",          "type": "text", "max": 100},
    {"name": "notion_last_edited", "type": "date"},
    {"name": "last_synced_at",     "type": "date"},
]


def ensure_pipeline_fields(pb: PBClient, collection: str) -> dict:
    """Ensure the 3 sync-pipeline fields + the unique notion_id index
    exist on the PB collection. Idempotent — skips fields/indexes already
    present. Returns a small report dict {fields_added: [...], index_added: bool}.

    Without this, the runner can't persist notion_id back to PB after
    creating a Notion page, so every sync pass duplicates the PB rows
    on the Notion side.
    """
    coll = _get_collection(pb, collection)
    fields = list(coll.get("fields") or [])
    existing_names = {f["name"] for f in fields}
    added: list[str] = []
    for spec in _PIPELINE_FIELDS:
        if spec["name"] in existing_names:
            continue
        fields.append(dict(spec))
        added.append(spec["name"])

    indexes = list(coll.get("indexes") or [])
    idx_name = f"idx_{collection}_notion_id"
    index_added = False
    if not any(idx_name in idx for idx in indexes):
        indexes.append(
            f"CREATE UNIQUE INDEX {idx_name} ON {collection} (notion_id) "
            f"WHERE notion_id != ''"
        )
        index_added = True

    if added or index_added:
        pb._http("PATCH", f"/api/collections/{collection}",  # noqa: SLF001
                  body={"fields": fields, "indexes": indexes})
    return {"fields_added": added, "index_added": index_added}


def provision_notion_db(
    *,
    pb: PBClient,
    nc: NotionClient,
    collection: str,
    title_field: str,
    db_title: str | None = None,
    parent_page_id: str | None = None,
) -> str:
    """Create a Notion database mirroring the PB collection schema."""
    parent_page_id = parent_page_id or os.environ.get("NOTION_SYNC_PARENT_PAGE_ID", "")
    if not parent_page_id:
        raise RuntimeError("NOTION_SYNC_PARENT_PAGE_ID not set")

    coll = _get_collection(pb, collection)
    fields = coll["fields"]
    field_by_name = {f["name"]: f for f in fields}
    if title_field not in field_by_name:
        raise RuntimeError(
            f"title_field={title_field!r} is not a field on PB "
            f"collection {collection!r}. Fields: {sorted(field_by_name)}"
        )

    # NEW: ensure pipeline fields exist on the PB collection. The
    # runner needs notion_id/notion_last_edited/last_synced_at to
    # persist links back to PB after each Notion write. Without these
    # the runner duplicates rows on every sync.
    ensure_pipeline_fields(pb, collection)

    properties: dict[str, dict] = {snake_to_title(title_field): {"title": {}}}
    targets = load_all(pb, fresh=True)
    for f in fields:
        name = f["name"]
        if name == title_field or name in _SYSTEM_FIELD_NAMES:
            continue
        notion_prop = _pb_field_to_notion_property_definition(
            f, pb=pb, all_targets=targets,
        )
        if notion_prop is None:
            continue
        properties[snake_to_title(name)] = notion_prop

    properties.setdefault("pb_id", {"rich_text": {}})
    properties.setdefault("last_synced_at", {"date": {}})

    db = nc.create_database(
        parent_page_id=parent_page_id,
        title=db_title or snake_to_title(collection),
        properties=properties,
    )
    try:
        add_collection_to_sync_activity(nc, collection=collection)
    except Exception as e:
        print(f"[provisioner] add_collection_to_sync_activity failed: {e}")
    return db["id"]


def add_collection_to_sync_activity(
    nc: NotionClient, *, collection: str
) -> None:
    """PATCH Sync Activity DB to include `collection` as a select option."""
    db_id = os.environ["NOTION_SYNC_ACTIVITY_DB_ID"]
    db = nc.retrieve_database(db_id)
    options = db["properties"]["collection"]["select"]["options"]
    if any(o.get("name") == collection for o in options):
        return
    new_options = options + [{"name": collection}]
    nc.update_database(db_id, {
        "properties": {"collection": {"select": {"options": new_options}}}
    })


def _get_collection(pb: PBClient, name: str) -> dict:
    return pb._http("GET", f"/api/collections/{name}")  # noqa: SLF001


def _pb_field_to_notion_property_definition(
    field: dict, *, pb: PBClient, all_targets: list,
) -> dict | None:
    """Return the Notion property body for one PB field, or None to skip."""
    ftype = field.get("type")
    name = field.get("name", "")

    if ftype in ("text", "editor", "autodate", "json"):
        return {"rich_text": {}}
    if ftype == "password":
        return None
    if ftype == "number":
        return {"number": {"format": "number"}}
    if ftype == "bool":
        return {"checkbox": {}}
    if ftype == "email":
        return {"email": {}}
    if ftype == "url":
        return {"url": {}}
    if ftype == "date":
        return {"date": {}}
    if ftype == "file":
        return {"files": {}}
    if ftype == "select":
        values = field.get("values", []) or []
        options = [{"name": v} for v in values]
        if int(field.get("maxSelect", 1) or 1) == 1:
            return {"select": {"options": options}}
        return {"multi_select": {"options": options}}
    if ftype == "relation":
        target_id = field.get("collectionId", "")
        if not target_id:
            return None
        try:
            target_coll = pb._http("GET", f"/api/collections/{target_id}")  # noqa: SLF001
            target_name = target_coll.get("name", "")
        except Exception:
            return None
        target = next((t for t in all_targets
                       if t.collection == target_name and t.enabled), None)
        if not target or not target.notion_db_id:
            print(f"[provisioner] skipping relation field {name!r} — "
                   f"target {target_name!r} is not synced")
            return None
        return {"relation": {
            "database_id": target.notion_db_id,
            "single_property": {},
        }}
    # Unknown type — safe fallback.
    print(f"[provisioner] unknown PB type {ftype!r} for field {name!r}; "
          f"falling back to rich_text")
    return {"rich_text": {}}
