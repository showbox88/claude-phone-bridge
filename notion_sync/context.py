"""Sync execution context.

Bundle the 6+ piece of per-collection state that flows through the sync
pipeline. Replaces the long kwarg-list signatures that previously
threaded these through every `_apply_*` function.

Constructed once at the start of `sync_collection`; immutable for the
duration of one collection's sync.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyncContext:
    """Per-collection state for one sync pass.

    Built by `sync_collection` once it has resolved overrides + notion
    schema + relation indexes for the target collection.
    """
    collection: str
    field_types: dict
    overrides: dict           # PB field name → Notion column name
    overrides_inv: dict       # reverse: Notion column → PB field
    title_field: str
    notion_schema: dict
    # Lazy relation indexes — None until first use. After Task 9,
    # `relation_lookup` will be a LazyRelationLookup instance.
    relation_lookup: object = None
    relation_targets: dict | None = None


def make_context(*,
                 collection: str,
                 field_types: dict,
                 overrides: dict,
                 title_field: str,
                 notion_schema: dict,
                 relation_lookup: object = None,
                 relation_targets: dict | None = None) -> SyncContext:
    """Convenience constructor that computes overrides_inv."""
    return SyncContext(
        collection=collection,
        field_types=field_types,
        overrides=overrides,
        overrides_inv={v: k for k, v in overrides.items()},
        title_field=title_field,
        notion_schema=notion_schema,
        relation_lookup=relation_lookup,
        relation_targets=relation_targets,
    )
