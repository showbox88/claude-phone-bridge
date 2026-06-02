"""PB ↔ Notion field-value conversion.

Field-name handling:
  - PB uses snake_case (`departure_time`).
  - Notion uses Title Case display names (`Departure Time`).
  - Automatic two-way mapping; special cases live in `field_map_overrides`
    on the sync_config row.

Value handling: PB stores flat JSON; Notion wraps each property in a typed
envelope. This module converts both ways. PB-side type comes from the
collection field spec (caller looks it up via list_collections()).
"""
from __future__ import annotations

from typing import Any


def _rich_text_str(item: dict) -> str:
    """Extract text from a rich_text item, tolerant of request vs response shape.

    Notion API responses include a flat ``plain_text`` field; request bodies
    we send don't (we send only ``{"text": {"content": "..."}}``). The codec
    must accept both so round-trip conversion works without simulating the
    plain_text addition.
    """
    pt = item.get("plain_text")
    if pt is not None:
        return pt
    return item.get("text", {}).get("content", "")


def snake_to_title(name: str) -> str:
    """departure_time -> Departure Time"""
    return " ".join(word.capitalize() for word in name.split("_"))


def title_to_snake(name: str) -> str:
    """Departure Time -> departure_time"""
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def pb_field_to_notion_property(value: Any, *,
                                pb_type: str,
                                max_select: int = 1) -> dict:
    """Convert a PB value to the body of a Notion property update."""
    if pb_type in ("text", "editor", "email", "url"):
        s = str(value or "")
        if pb_type == "email":
            return {"email": s or None}
        if pb_type == "url":
            return {"url": s or None}
        if not s:
            return {"rich_text": []}
        return {"rich_text": [{"type": "text", "text": {"content": s}}]}

    if pb_type == "number":
        return {"number": value if value is not None else None}

    if pb_type == "bool":
        return {"checkbox": bool(value)}

    if pb_type == "date":
        if not value:
            return {"date": None}
        date_part = str(value).split(" ")[0].split("T")[0]
        return {"date": {"start": date_part}}

    if pb_type == "select":
        if max_select == 1:
            return {"select": {"name": str(value)} if value else None}
        items = value if isinstance(value, list) else ([value] if value else [])
        return {"multi_select": [{"name": str(v)} for v in items]}

    if pb_type == "relation":
        ids = value if isinstance(value, list) else ([value] if value else [])
        return {"relation": [{"id": i} for i in ids if i]}

    if pb_type == "json":
        import json as _json
        return {"rich_text": [{"type": "text",
                                "text": {"content": _json.dumps(value, ensure_ascii=False)}}]}

    return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}


def notion_property_to_pb_field(prop: dict, *,
                                pb_type: str,
                                max_select: int = 1) -> Any:
    """Convert a Notion property (API response shape) to a PB value."""
    ntype = prop.get("type")

    if ntype == "title":
        return "".join(_rich_text_str(rt) for rt in prop.get("title", []))

    if ntype == "rich_text":
        return "".join(_rich_text_str(rt) for rt in prop.get("rich_text", []))

    if ntype == "number":
        return prop.get("number")

    if ntype == "checkbox":
        return bool(prop.get("checkbox"))

    if ntype == "email":
        return prop.get("email") or ""

    if ntype == "url":
        return prop.get("url") or ""

    if ntype == "date":
        d = prop.get("date")
        if not d:
            return ""
        return d.get("start", "")

    if ntype == "select":
        s = prop.get("select")
        return (s or {}).get("name", "")

    if ntype == "multi_select":
        return [item.get("name", "") for item in prop.get("multi_select", [])]

    if ntype == "relation":
        return [r.get("id", "") for r in prop.get("relation", [])]

    if ntype == "people":
        return [p.get("id", "") for p in prop.get("people", [])]

    if ntype == "files":
        return [f.get("name", "") for f in prop.get("files", [])]

    return prop
