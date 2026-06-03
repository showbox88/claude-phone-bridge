"""PB ↔ Notion row-level transforms — shared by reconcile_initial and runner.

PR2 limitation: relation fields are NOT translated between sides. Notion
stores its own page UUIDs as relation targets; PB stores its own record
IDs. The two ID spaces don't match, so blindly copying a Notion UUID
into PB's relation field fails validation. Until a future PR implements
the ID lookup, this module skips relation fields in BOTH directions —
PB relations stay at whatever PR1 reconcile aligned them to; user edits
to relation fields in either UI do not propagate.
"""
from __future__ import annotations

from notion_sync.codec import (
    notion_property_to_pb_field,
    pb_field_to_notion_property,
    snake_to_title,
    title_to_snake,
)
from notion_sync.pb_api import PBClient


def collection_field_types(pb: PBClient, name: str) -> dict[str, dict]:
    for c in pb.list_collections():
        if c["name"] == name:
            return {
                f["name"]: {"type": f["type"], "maxSelect": f.get("maxSelect", 1)}
                for f in c.get("fields", [])
            }
    raise RuntimeError(f"collection not found: {name}")


def notion_page_to_pb_dict(page: dict, field_types: dict[str, dict],
                           overrides: dict[str, str]) -> dict:
    out: dict = {}
    for prop_name, prop_val in page.get("properties", {}).items():
        pb_name = overrides.get(prop_name, title_to_snake(prop_name))
        if pb_name not in field_types:
            continue
        spec = field_types[pb_name]
        # PR2: skip relation fields — Notion holds Notion UUIDs but PB
        # expects PB record IDs. Cross-ID translation is a future PR.
        if spec["type"] == "relation":
            continue
        out[pb_name] = notion_property_to_pb_field(
            prop_val, pb_type=spec["type"], max_select=spec.get("maxSelect", 1)
        )
    return out


def pb_record_to_notion_props(record: dict, field_types: dict[str, dict],
                              overrides_inv: dict[str, str],
                              title_field: str,
                              notion_schema: dict[str, dict]) -> dict:
    SKIP = {"id", "created", "updated", "collectionId", "collectionName",
            "expand", "notion_id", "notion_last_edited", "last_synced_at"}
    notion_by_snake = {title_to_snake(name): name for name in notion_schema}
    title_prop_name = next(
        (n for n, s in notion_schema.items() if s.get("type") == "title"),
        None,
    )

    props: dict = {}
    for pb_name, value in record.items():
        if pb_name in SKIP:
            continue
        if pb_name not in field_types:
            continue
        if pb_name == title_field:
            continue
        spec = field_types[pb_name]
        # PR2: skip relation fields — cross-ID translation deferred.
        if spec["type"] == "relation":
            continue
        notion_name = overrides_inv.get(pb_name) or notion_by_snake.get(pb_name)
        if not notion_name or notion_name not in notion_schema:
            continue
        notion_type = notion_schema[notion_name].get("type")
        props[notion_name] = pb_field_to_notion_property(
            value,
            pb_type=spec["type"],
            max_select=spec.get("maxSelect", 1),
            notion_type=notion_type,
        )

    if title_prop_name is not None:
        title_val = record.get(title_field, "") or ""
        props[title_prop_name] = {"title": [{"type": "text",
                                              "text": {"content": str(title_val)[:200]}}]}

    return props
