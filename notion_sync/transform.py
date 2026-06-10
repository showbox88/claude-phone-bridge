"""PB ↔ Notion row-level transforms — shared by reconcile_initial and runner.

PB→Notion relation translation: when caller supplies `relation_lookup` +
`relation_targets`, `pb_record_to_notion_props` translates PB relation
ids to Notion page UUIDs by looking up the target collection's pipeline
`notion_id` field. Notion→PB relation translation is still skipped
(needs the inverse lookup; user edits to relation columns on Notion side
do not propagate to PB).
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


def relation_target_collections(pb: PBClient, collection: str) -> dict[str, str]:
    """For each relation field on `collection`, return its target collection name.

    Maps `{relation_field_name: target_collection_name}` so the caller can
    look up the right pipeline map when translating PB ids to Notion ids.
    """
    cols = pb.list_collections()
    src = next((c for c in cols if c["name"] == collection), None)
    if src is None:
        raise RuntimeError(f"collection not found: {collection}")
    by_id = {c["id"]: c["name"] for c in cols}
    out: dict[str, str] = {}
    for f in src.get("fields", []):
        if f.get("type") != "relation":
            continue
        target_id = f.get("collectionId")
        if not target_id or target_id not in by_id:
            continue
        out[f["name"]] = by_id[target_id]
    return out


class LazyRelationLookup:
    """Memoized per-target `{pb_id: notion_id}` index.

    Replaces eager `build_relation_lookup`. First `.get(target)` /
    `lookup[target]` triggers a PB `list_records` for that target;
    subsequent calls return the cached dict. Collections that never
    reference relations incur zero PB fetches.

    Phase 5 Task 9: cuts cold-start PB calls at sync_collection from
    N (every enabled target) to just the relation targets actually
    referenced by the source collection being synced.

    Contract matches the legacy dict: `lookup.get(target, default)` returns
    `default` (typically `{}`) when the target either errors during fetch
    or isn't a known collection — same silent-skip behaviour the eager
    version had via `try/except continue`.
    """

    def __init__(self, pb: PBClient):
        self._pb = pb
        self._cache: dict[str, dict[str, str]] = {}
        self._failed: set[str] = set()

    def _load(self, target: str) -> dict[str, str] | None:
        if target in self._cache:
            return self._cache[target]
        if target in self._failed:
            return None
        try:
            rows = self._pb.list_records(target, sort="")
        except Exception:
            self._failed.add(target)
            return None
        idx = {r["id"]: r["notion_id"] for r in rows if r.get("notion_id")}
        self._cache[target] = idx
        return idx

    def get(self, target: str, default=None):
        """Dict-style: return `{pb_id: notion_id}` for `target`, or `default`
        if the target cannot be fetched. Caches successful fetches and
        remembers failures so retries don't re-hit PB.
        """
        idx = self._load(target)
        return idx if idx is not None else default

    def __getitem__(self, target: str) -> dict[str, str]:
        idx = self._load(target)
        if idx is None:
            raise KeyError(target)
        return idx

    def __contains__(self, target: str) -> bool:
        return self._load(target) is not None


def build_relation_lookup(pb: PBClient, collections: list[str]) -> dict[str, dict[str, str]]:
    """Eager-fetch `{collection: {pb_id: notion_id}}` for all `collections`.

    Kept as a thin back-compat wrapper around `LazyRelationLookup` for
    callers (e.g. `scripts/reconcile_initial.py`, `scripts/backfill_relations.py`)
    that want a plain dict up front. The runtime sync path uses
    `LazyRelationLookup` directly (Phase 5 Task 9).

    Only rows whose `notion_id` is non-empty are included — rows not yet
    synced to Notion can't be translated. Collections that error during
    fetch are silently skipped (absent from the returned dict).
    """
    lazy = LazyRelationLookup(pb)
    out: dict[str, dict[str, str]] = {}
    for c in collections:
        idx = lazy.get(c)
        if idx is not None:
            out[c] = idx
    return out


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
                              notion_schema: dict[str, dict],
                              relation_lookup: dict[str, dict[str, str]] | None = None,
                              relation_targets: dict[str, str] | None = None) -> dict:
    """PB row → Notion property dict.

    When `relation_lookup` (`{collection: {pb_id: notion_id}}`) and
    `relation_targets` (`{relation_field_name: target_collection}`) are both
    supplied, relation fields are translated to Notion `relation` properties.
    PB ids that don't resolve to a Notion page (unsynced target) are dropped
    silently; an empty relation list still clears the column on Notion side.
    """
    SKIP = {"id", "created", "updated", "collectionId", "collectionName",
            "expand", "notion_id", "notion_last_edited", "last_synced_at"}
    notion_by_snake = {title_to_snake(name): name for name in notion_schema}
    title_prop_name = next(
        (n for n, s in notion_schema.items() if s.get("type") == "title"),
        None,
    )

    can_translate_relations = relation_lookup is not None and relation_targets is not None

    # Per-row tz hint for datetime fields. Priority: the row's own
    # `timezone` column (locations/stops/days/expenses/foods) OR `due_tz`
    # (todos). Empty disables the offset hint (legacy UTC).
    row_tz = (record.get("timezone") or record.get("due_tz") or "").strip() or None

    props: dict = {}
    for pb_name, value in record.items():
        if pb_name in SKIP:
            continue
        if pb_name not in field_types:
            continue
        if pb_name == title_field:
            continue
        spec = field_types[pb_name]
        notion_name = overrides_inv.get(pb_name) or notion_by_snake.get(pb_name)
        if not notion_name or notion_name not in notion_schema:
            continue

        if spec["type"] == "relation":
            if not can_translate_relations:
                continue
            target_col = relation_targets.get(pb_name)
            if not target_col:
                continue
            if notion_schema[notion_name].get("type") != "relation":
                continue
            target_map = relation_lookup.get(target_col, {})
            if isinstance(value, str):
                pb_ids = [value] if value else []
            elif isinstance(value, list):
                pb_ids = [v for v in value if v]
            else:
                pb_ids = []
            notion_refs = [{"id": target_map[pid]} for pid in pb_ids if pid in target_map]
            props[notion_name] = {"relation": notion_refs}
            continue

        notion_type = notion_schema[notion_name].get("type")
        if spec["type"] == "date":
            props[notion_name] = pb_field_to_notion_property(
                value,
                pb_type="date",
                max_select=spec.get("maxSelect", 1),
                notion_type=notion_type,
                tz=row_tz,
            )
        else:
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
