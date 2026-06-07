"""Canonical tool descriptions + JSON-Schema arg schemas for the PB MCP surface.

Both in-process (pb_tools.py with claude_agent_sdk's @tool) and external
(mcp_pb/server.py with FastMCP @mcp.tool()) servers source description
strings + schemas from this module to keep the two MCP entry points
exactly in sync.

To update a description: edit here; both servers pick it up next deploy.

Descriptions captured verbatim from pb_tools.py (the canonical wording
as of Phase 0). Schemas are JSON Schema objects matching what the SDK
MCP `@tool(name, description, schema)` decorator expects; FastMCP can
translate the same shape via parameter introspection.

Scope: the 11 tools that are mirrored in BOTH MCP servers. The
pb_tools-only sync_* business tools (sync_now / sync_queue_status /
sync_pause / sync_resume) are NOT here — they don't have a mcp_pb
counterpart, so there's nothing to keep in sync.
"""
from __future__ import annotations


TOOL_DESCRIPTIONS: dict[str, str] = {
    "pb_list_collections": (
        "List all PocketBase collections with their fields and (for select fields) "
        "valid values. Call this at the start of a Smart Note conversation so you "
        "know the current schema and pick the right collection / select option."
    ),
    "pb_search": (
        "Search records in a PocketBase collection.\n\n"
        "Filter uses PB DSL: (field='value' && other!=0). Examples:\n"
        "  - status='Active' && priority='High'\n"
        "  - title~'idea'           (~ = LIKE)\n"
        "  - date >= '2026-01-01'\n"
        "Sort: comma list with '-' prefix for desc, e.g. '-date,title'.\n"
        "Expand: comma list of relation field names whose target records to embed."
    ),
    "pb_get": (
        "Get a single PocketBase record by ID, optionally with 'expand' for relations."
    ),
    "pb_create": (
        "Create a record in a PocketBase collection. 'data' is a field map.\n\n"
        "PB auto-fills id, created, updated. For select fields use the exact string "
        "value (case-sensitive). For relation fields use the target record's id "
        "(single) or list of ids (multi)."
    ),
    "pb_update": (
        "Update specific fields of a PocketBase record. Pass only fields to change.\n\n"
        "Common patterns:\n"
        "  - Archive: pb_update(coll, id, {\"status\": \"Archived\"})\n"
        "  - Mark todo done: pb_update(\"todos\", id, {\"status\": \"Done\", "
        "\"completed_at\": \"2026-05-27\"})"
    ),
    "pb_delete": (
        "Permanently delete a PocketBase record. Irreversible. Per Smart Note rules, "
        "prefer pb_update(coll, id, {\"status\": \"Archived\"}) for normal mistakes. "
        "Use real delete only when the user explicitly asks (\"hard delete\", "
        "\"really remove\", \"彻底删掉\"), or for obvious garbage like duplicate rows / "
        "test scaffolding / records the user never saw."
    ),
    "pb_get_collection": (
        "Fetch the full definition of one collection (all fields with their raw "
        "config). Use before pb_update_collection to read the current field array, "
        "then mutate and patch it back."
    ),
    "pb_create_collection": (
        "Create a new PocketBase collection (table). 'fields' is a list of field-spec "
        "dicts; each needs at minimum 'name' and 'type'. Common types: text, editor "
        "(markdown), number, bool, date, email, url, select "
        "({\"type\":\"select\",\"maxSelect\":1,\"values\":[...]}), relation "
        "({\"type\":\"relation\",\"collectionId\":\"<id>\",\"maxSelect\":1}), json, "
        "file. PB auto-adds id/created/updated. Returns the created collection."
    ),
    "pb_update_collection": (
        "Patch an existing collection (rename, add/remove/modify fields, indexes, "
        "rules). 'patch' is merged onto the current definition. To add a field, "
        "include the FULL fields array (existing + new) — read it first with "
        "pb_get_collection. Existing fields keep their data; new fields default to "
        "null for old rows."
    ),
    "pb_delete_collection": (
        "Delete a collection AND all its records. Irreversible. Use only when "
        "explicitly asked by the user."
    ),
    "smartnote_open_context": (
        "Fetch active high-priority memos from claude_memos. Call at the start of a "
        "Smart Note conversation to recover persistent context."
    ),
}


TOOL_SCHEMAS: dict[str, dict] = {
    "pb_list_collections": {},
    "pb_search": {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "description": "Collection name"},
            "filter":     {"type": "string", "description": "PB filter DSL (optional)"},
            "sort":       {"type": "string", "description": "Sort spec, default '-created'"},
            "expand":     {"type": "string", "description": "Relation fields to embed (optional)"},
            "page":       {"type": "integer", "description": "1-based page, default 1"},
            "per_page":   {"type": "integer", "description": "Page size 1-200, default 30"},
        },
        "required": ["collection"],
    },
    "pb_get": {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "id":         {"type": "string"},
            "expand":     {"type": "string", "description": "Relation fields to embed (optional)"},
        },
        "required": ["collection", "id"],
    },
    "pb_create": {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "data":       {"type": "object", "description": "Field map for the new record"},
        },
        "required": ["collection", "data"],
    },
    "pb_update": {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "id":         {"type": "string"},
            "data":       {"type": "object", "description": "Fields to change"},
        },
        "required": ["collection", "id", "data"],
    },
    "pb_delete": {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "id":         {"type": "string"},
        },
        "required": ["collection", "id"],
    },
    "pb_get_collection": {
        "type": "object",
        "properties": {"id_or_name": {"type": "string"}},
        "required": ["id_or_name"],
    },
    "pb_create_collection": {
        "type": "object",
        "properties": {
            "name":   {"type": "string"},
            "fields": {"type": "array", "items": {"type": "object"},
                       "description": "List of field-spec dicts"},
            "type":   {"type": "string", "description": "Collection type, default 'base'"},
        },
        "required": ["name", "fields"],
    },
    "pb_update_collection": {
        "type": "object",
        "properties": {
            "id_or_name": {"type": "string"},
            "patch":      {"type": "object", "description": "Fields to merge onto the collection"},
        },
        "required": ["id_or_name", "patch"],
    },
    "pb_delete_collection": {
        "type": "object",
        "properties": {"id_or_name": {"type": "string"}},
        "required": ["id_or_name"],
    },
    "smartnote_open_context": {},
}
