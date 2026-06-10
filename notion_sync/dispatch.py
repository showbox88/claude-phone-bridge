"""Per-collection sync dispatch.

`sync_collection` is the per-collection entry-point called by the main
runner loop AND by external scripts (scripts/reconcile_initial.py).
It categorizes PB↔Notion deltas into Action objects, then either:
  - applies them via ACTION_HANDLERS (one of the 4 apply-fns), or
  - writes a Sync Activity row for conflicts/vanishes (manual review), or
  - skips frozen rows that already have a Pending Sync Activity row.

Phase 5 Task 14 split this out of runner.py.
"""
from __future__ import annotations

import traceback

from notion_sync.activity import (
    frozen_pairs_for_collection,
    write_conflict,
    write_delete_question,
)
from notion_sync.changeset import (
    BothChanged,
    NoChange,
    NotionNew,
    NotionOnlyChange,
    NotionVanished,
    PbNew,
    PbOnlyChange,
    PbVanished,
    categorize,
)
from notion_sync.decisions import apply_pending_decisions
from notion_sync.icons import icon_for
from notion_sync.logger import log_event
from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.transform import (
    LazyRelationLookup,
    collection_field_types,
    notion_page_to_pb_dict,
    pb_record_to_notion_props,
    relation_target_collections,
)


def _now_iso_date() -> str:
    # Local indirection to avoid module-load circular: bootstrap imports
    # this module. Both helpers live in bootstrap.py; we lazy-import.
    from notion_sync.bootstrap import now_iso_date
    return now_iso_date()


def _now_iso_datetime() -> str:
    from notion_sync.bootstrap import now_iso_datetime
    return now_iso_datetime()


def _pb_id_from_notion(page: dict) -> str:
    prop = page.get("properties", {}).get("pb_id", {})
    return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))


# Action ID extraction table. Maps each Action class to (pb_id_getter,
# notion_id_getter) lambdas. Replaces a long isinstance chain so adding
# a new Action class fails loudly (test_every_action_class_in_table
# guards) instead of silently returning (None, None).
_ACTION_ID_GETTERS = {
    NoChange:         (lambda a: a.pb_id,
                       lambda a: a.notion_id),
    PbOnlyChange:     (lambda a: a.pb_row["id"],
                       lambda a: a.notion_id),
    NotionOnlyChange: (lambda a: a.pb_id,
                       lambda a: a.notion_page["id"]),
    BothChanged:      (lambda a: a.pb_row["id"],
                       lambda a: a.notion_page["id"]),
    PbNew:            (lambda a: a.pb_row["id"],
                       lambda a: None),
    NotionNew:        (lambda a: None,
                       lambda a: a.notion_page["id"]),
    NotionVanished:   (lambda a: a.pb_row["id"],
                       lambda a: a.pb_row.get("notion_id") or None),
    PbVanished:       (lambda a: _pb_id_from_notion(a.notion_page) or None,
                       lambda a: a.notion_page["id"]),
}


def _action_ids(a) -> tuple[str | None, str | None]:
    """Return (pb_id, notion_id) for any Action. Returns (None, None)
    for unknown types (the caller logs the type separately).

    Either side may be None if that side doesn't exist (e.g. PbNew has
    no notion_id yet; NotionVanished has a 'missing' notion_id stored
    on the PB row).
    """
    pair = _ACTION_ID_GETTERS.get(type(a))
    if pair is None:
        return (None, None)
    pb_getter, notion_getter = pair
    try:
        return (pb_getter(a), notion_getter(a))
    except (AttributeError, KeyError, TypeError):
        return (None, None)


def _apply_pb_to_notion(action: PbOnlyChange, *,
                        collection: str,
                        field_types: dict,
                        overrides_inv: dict,
                        title_field: str,
                        notion_schema: dict,
                        relation_lookup: dict | None,
                        relation_targets: dict | None,
                        icon_field: str | None,
                        icon_default: str | None,
                        pb: PBClient, nc: NotionClient) -> None:
    r = action.pb_row
    props = pb_record_to_notion_props(r, field_types, overrides_inv,
                                       title_field, notion_schema,
                                       relation_lookup=relation_lookup,
                                       relation_targets=relation_targets)
    props["last_synced_at"] = {"date": {"start": _now_iso_date()}}
    page = nc.update_page(action.notion_id, properties=props,
                          icon=icon_for(collection, r,
                                        icon_field=icon_field,
                                        icon_default=icon_default))
    pb.update_record(collection, r["id"], {
        "notion_last_edited": page.get("last_edited_time"),
        "last_synced_at": _now_iso_datetime(),
    })


def _apply_notion_to_pb(action: NotionOnlyChange, *,
                        collection: str,
                        field_types: dict,
                        overrides: dict,
                        title_field: str,
                        pb: PBClient, nc: NotionClient) -> None:
    npage = action.notion_page
    npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
    pb.update_record(collection, action.pb_id, npage_dict | {
        "notion_last_edited": npage.get("last_edited_time"),
        "last_synced_at": _now_iso_datetime(),
    })


def _apply_pb_new(action: PbNew, *,
                  collection: str,
                  notion_db_id: str,
                  field_types: dict,
                  overrides_inv: dict,
                  title_field: str,
                  notion_schema: dict,
                  relation_lookup: dict | None,
                  relation_targets: dict | None,
                  icon_field: str | None,
                  icon_default: str | None,
                  pb: PBClient, nc: NotionClient) -> None:
    r = action.pb_row
    props = pb_record_to_notion_props(r, field_types, overrides_inv,
                                       title_field, notion_schema,
                                       relation_lookup=relation_lookup,
                                       relation_targets=relation_targets)
    props["pb_id"] = {"rich_text": [{"type": "text", "text": {"content": r["id"]}}]}
    props["last_synced_at"] = {"date": {"start": _now_iso_date()}}
    page = nc.create_page(notion_db_id, props,
                          icon=icon_for(collection, r,
                                        icon_field=icon_field,
                                        icon_default=icon_default))
    pb.update_record(collection, r["id"], {
        "notion_id": page["id"],
        "notion_last_edited": page.get("last_edited_time"),
        "last_synced_at": _now_iso_datetime(),
    })


def _apply_notion_new(action: NotionNew, *,
                      collection: str,
                      field_types: dict,
                      overrides: dict,
                      title_field: str,
                      pb: PBClient, nc: NotionClient) -> None:
    npage = action.notion_page
    npage_dict = notion_page_to_pb_dict(npage, field_types, overrides)
    created = pb.create_record(collection, npage_dict | {
        "notion_id": npage["id"],
        "notion_last_edited": npage.get("last_edited_time"),
        "last_synced_at": _now_iso_datetime(),
    })
    nc.update_page(npage["id"], properties={
        "pb_id": {"rich_text": [{"type": "text",
                                  "text": {"content": created["id"]}}]},
        "last_synced_at": {"date": {"start": _now_iso_date()}},
    })


# Apply-fn dispatch table. Replaces the isinstance chain for the four
# Action types that map 1:1 to a single apply-fn. NoChange / BothChanged
# / Vanished have non-apply-fn handling and stay inline in sync_collection.
ACTION_HANDLERS = {
    PbOnlyChange:     _apply_pb_to_notion,
    NotionOnlyChange: _apply_notion_to_pb,
    PbNew:            _apply_pb_new,
    NotionNew:        _apply_notion_new,
}


def sync_collection(cfg_row: dict, pb: PBClient, nc: NotionClient,
                    *, frozen_pairs: tuple[set[str], set[str]] | None = None
                    ) -> dict:
    """Sync one collection.

    frozen_pairs: pre-computed (frozen_pb_ids, frozen_notion_ids) for
    this collection from the batched main()-level fetch. If None,
    falls back to the legacy single-collection query — keeps external
    callers like scripts/reconcile_initial.py working unchanged.
    """
    collection = cfg_row["collection"]
    notion_db_id = cfg_row["notion_db_id"]
    overrides = cfg_row.get("field_map_overrides") or {}
    overrides_inv = {v: k for k, v in overrides.items()}
    last_synced_at = cfg_row.get("last_synced_at") or ""
    # Declarative icon hints (Phase 5 Task 5). Only consulted by icon_for
    # for collections WITHOUT a legacy domain mapping. NULL/missing is
    # safe — legacy collections ignore these entirely.
    icon_field = cfg_row.get("icon_field") or None
    icon_default = cfg_row.get("icon_default") or None

    field_types = collection_field_types(pb, collection)
    title_field = cfg_row.get("title_field") or ""
    if not title_field:
        raise RuntimeError(
            f"sync_config[{collection}].title_field is empty — set it via "
            f"the settings UI or PB admin before this collection can sync"
        )

    notion_db = nc.retrieve_database(notion_db_id)
    notion_schema = notion_db.get("properties", {})

    # Build PB→Notion relation lookup once per sync_collection call.
    # Phase 5 Task 9: lazy — only the relation targets actually referenced
    # by `collection`'s relation_targets are fetched, and only on first use.
    # Fresh rows added DURING this pass don't appear in the lookup, but
    # they'll be linkable on the next pass — acceptable for initial
    # relation backfill.
    relation_lookup = LazyRelationLookup(pb)
    relation_targets = relation_target_collections(pb, collection)

    # Phase 0: apply user-decided Sync Activity rows. After this, applied
    # rows have applied_at set and won't appear in the freeze set below.
    decisions_applied = apply_pending_decisions(
        pb, nc, collection=collection,
        field_types=field_types, overrides=overrides,
        overrides_inv=overrides_inv, title_field=title_field,
        notion_schema=notion_schema,
        relation_lookup=relation_lookup,
        relation_targets=relation_targets,
        icon_field=icon_field,
        icon_default=icon_default,
    )

    pb_rows = pb.list_records(collection, sort="")
    notion_rows = nc.query_database(notion_db_id)
    actions = categorize(pb_rows, notion_rows, last_synced_at=last_synced_at)

    # Freeze: rows with a Pending Conflict or Delete? in Sync Activity
    # are off-limits until the user picks a decision. The runner skips
    # any action whose pb_id or notion_id appears in either set, no
    # matter what category it falls into. Prevents subsequent edits
    # from cascading into NotionOnlyChange / PbOnlyChange / etc and
    # silently overwriting the conflicted side before the user decides.
    #
    # main() pre-fetches frozen pairs for all enabled collections in
    # one group-by query and passes the per-collection slice here. When
    # called from outside main() (e.g. reconcile_initial.py), fall back
    # to the legacy single-collection query so external callers don't
    # need to know about the batching.
    if frozen_pairs is None:
        frozen_pb_ids, frozen_notion_ids = frozen_pairs_for_collection(
            nc, collection=collection,
        )
    else:
        frozen_pb_ids, frozen_notion_ids = frozen_pairs

    # Counts are tallied AFTER the freeze check so frozen rows don't
    # inflate applied/conflict/delete counts in the log.
    counts: dict[str, int] = {}
    skipped_frozen = 0

    # Per-handler kwargs (each apply-fn uses a different subset, so we
    # can't just unpack one big dict).
    pb_to_notion_kwargs = dict(
        collection=collection, field_types=field_types,
        overrides_inv=overrides_inv, title_field=title_field,
        notion_schema=notion_schema,
        relation_lookup=relation_lookup,
        relation_targets=relation_targets,
        icon_field=icon_field, icon_default=icon_default,
        pb=pb, nc=nc,
    )
    notion_to_pb_kwargs = dict(
        collection=collection, field_types=field_types,
        overrides=overrides, title_field=title_field,
        pb=pb, nc=nc,
    )
    pb_new_kwargs = dict(pb_to_notion_kwargs, notion_db_id=notion_db_id)
    notion_new_kwargs = notion_to_pb_kwargs

    handler_kwargs = {
        PbOnlyChange:     pb_to_notion_kwargs,
        NotionOnlyChange: notion_to_pb_kwargs,
        PbNew:            pb_new_kwargs,
        NotionNew:        notion_new_kwargs,
    }

    for a in actions:
        try:
            pid, nid = _action_ids(a)
            if (pid and pid in frozen_pb_ids) or (nid and nid in frozen_notion_ids):
                skipped_frozen += 1
                continue
            counts[type(a).__name__] = counts.get(type(a).__name__, 0) + 1

            if isinstance(a, NoChange):
                continue

            if isinstance(a, BothChanged):
                # First detection only — re-detection is short-circuited
                # by the outer freeze check above.
                pb_id = a.pb_row["id"]
                notion_id = a.notion_page["id"]
                notion_dict = notion_page_to_pb_dict(
                    a.notion_page, field_types, overrides,
                )
                write_conflict(
                    nc,
                    collection=collection,
                    summary=str(a.pb_row.get(title_field, ""))[:120],
                    pb_id=pb_id, notion_id=notion_id,
                    pb_snapshot=a.pb_row,
                    notion_snapshot=notion_dict,
                    record_link=a.notion_page.get("url"),
                )
                continue

            if isinstance(a, NotionVanished):
                # First detection only.
                pb_id = a.pb_row["id"]
                missing_nid = a.pb_row.get("notion_id") or ""
                write_delete_question(
                    nc,
                    collection=collection,
                    summary=("Notion page missing: "
                             + str(a.pb_row.get(title_field, ""))[:80]),
                    pb_id=pb_id, notion_id=missing_nid,
                    snapshot=a.pb_row,
                )
                continue

            if isinstance(a, PbVanished):
                # First detection only.
                missing_pid = _pb_id_from_notion(a.notion_page)
                notion_id = a.notion_page["id"]
                notion_dict = notion_page_to_pb_dict(
                    a.notion_page, field_types, overrides,
                )
                write_delete_question(
                    nc,
                    collection=collection,
                    summary=("PB record missing: "
                             + str(notion_dict.get(title_field, ""))[:80]),
                    pb_id=missing_pid, notion_id=notion_id,
                    snapshot=notion_dict,
                )
                continue

            handler = ACTION_HANDLERS.get(type(a))
            if handler is None:
                # Unknown action type — log and skip rather than crash.
                pb_id, notion_id = _action_ids(a)
                log_event("unknown_action", collection=collection,
                          action=type(a).__name__,
                          pb_id=pb_id, notion_id=notion_id)
                continue
            handler(a, **handler_kwargs[type(a)])
        except Exception as e:
            pb_id, notion_id = _action_ids(a)
            log_event("apply_error",
                      collection=collection,
                      action=type(a).__name__,
                      pb_id=pb_id,
                      notion_id=notion_id,
                      error=str(e),
                      trace=traceback.format_exc()[:1000])

    return {
        "counts": counts,
        "applied": (counts.get("PbOnlyChange", 0)
                    + counts.get("NotionOnlyChange", 0)
                    + counts.get("PbNew", 0)
                    + counts.get("NotionNew", 0)),
        "conflicts": counts.get("BothChanged", 0),
        "deletes": counts.get("NotionVanished", 0) + counts.get("PbVanished", 0),
        "frozen_skipped": skipped_frozen,
        "decisions_applied": decisions_applied,
    }
