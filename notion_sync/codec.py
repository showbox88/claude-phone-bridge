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

import json as _json
from typing import Any


def _pb_date_to_notion_start(value: Any) -> str:
    """Convert a PB date/datetime string into the value for Notion's date.start.

    PB stores datetime in ``date`` fields as ``"YYYY-MM-DD HH:MM:SS.fffZ"``. When
    the HH:MM:SS portion is non-zero we emit a full ISO 8601 string so Notion
    treats the property as a datetime (``is_datetime=1``); when it's zero we
    emit ``YYYY-MM-DD`` so Notion keeps it as date-only. Returns "" for empty
    input.

    Edge case: a real event at exactly 00:00:00 UTC is indistinguishable from
    a date-only value and will be shown date-only in Notion. The full timestamp
    still round-trips through PB intact.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    s_norm = s.replace("T", " ")
    parts = s_norm.split(" ", 1)
    date_part = parts[0]
    if len(parts) < 2:
        return date_part
    time_part = parts[1].rstrip().rstrip("Z").rstrip()
    hms = time_part.split(".", 1)[0]
    if not hms or hms == "00:00:00":
        return date_part
    return f"{date_part}T{hms}Z"


def _rich_text_str(item: dict) -> str:
    """Extract text from a rich_text item, tolerant of request vs response shape.

    Notion API responses include a flat ``plain_text`` field; request bodies
    we send don't (we send only ``{"text": {"content": "..."}}``). The codec
    must accept both so round-trip conversion works without simulating the
    plain_text addition. Returns "" for non-dict items (defensive against
    formula/rollup oddities).
    """
    if not isinstance(item, dict):
        return ""
    pt = item.get("plain_text")
    if pt is not None:
        return pt
    return item.get("text", {}).get("content", "")


def snake_to_title(name: str) -> str:
    """departure_time -> Departure Time. Skips empty segments from consecutive underscores."""
    return " ".join(word.capitalize() for word in name.split("_") if word)


def title_to_snake(name: str) -> str:
    """Departure Time -> departure_time"""
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def pb_field_to_notion_property(value: Any, *,
                                pb_type: str,
                                max_select: int = 1,
                                notion_type: str | None = None) -> dict:
    """Convert a PB value to the body of a Notion property update.

    If ``notion_type`` is given, the envelope is chosen by Notion's property
    type (so a PB ``text`` field whose Notion column is ``phone_number`` is
    encoded correctly). Otherwise the encoding is inferred from ``pb_type``,
    preserving the original behavior.
    """
    # Notion-type-driven path: trust the destination schema.
    if notion_type is not None:
        s_value = "" if value is None else str(value)
        if notion_type == "title":
            if not s_value:
                return {"title": []}
            return {"title": [{"type": "text", "text": {"content": s_value[:2000]}}]}
        if notion_type == "rich_text":
            if not s_value:
                return {"rich_text": []}
            return {"rich_text": [{"type": "text", "text": {"content": s_value[:2000]}}]}
        if notion_type == "number":
            if value is None or value == "":
                return {"number": None}
            try:
                return {"number": float(value) if isinstance(value, str) else value}
            except (TypeError, ValueError):
                return {"number": None}
        if notion_type == "checkbox":
            return {"checkbox": bool(value)}
        if notion_type == "date":
            if not s_value:
                return {"date": None}
            start = _pb_date_to_notion_start(s_value)
            return {"date": {"start": start}} if start else {"date": None}
        if notion_type == "select":
            return {"select": {"name": s_value} if s_value else None}
        if notion_type == "multi_select":
            items = value if isinstance(value, list) else ([value] if value else [])
            return {"multi_select": [{"name": str(v)} for v in items if v]}
        if notion_type == "relation":
            ids = value if isinstance(value, list) else ([value] if value else [])
            return {"relation": [{"id": i} for i in ids if i]}
        if notion_type == "email":
            return {"email": s_value or None}
        if notion_type == "url":
            return {"url": s_value or None}
        if notion_type == "phone_number":
            return {"phone_number": s_value or None}
        if notion_type == "people":
            ids = value if isinstance(value, list) else ([value] if value else [])
            return {"people": [{"id": i} for i in ids if i]}
        # Unknown Notion type — encode as best-effort rich_text so we don't crash.
        return {"rich_text": [{"type": "text", "text": {"content": s_value}}]} if s_value else {"rich_text": []}

    # Fallback PB-type-driven path (original behavior).
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
        start = _pb_date_to_notion_start(value)
        return {"date": {"start": start}} if start else {"date": None}

    if pb_type == "select":
        if max_select == 1:
            return {"select": {"name": str(value)} if value else None}
        items = value if isinstance(value, list) else ([value] if value else [])
        return {"multi_select": [{"name": str(v)} for v in items]}

    if pb_type == "relation":
        ids = value if isinstance(value, list) else ([value] if value else [])
        return {"relation": [{"id": i} for i in ids if i]}

    if pb_type == "json":
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
