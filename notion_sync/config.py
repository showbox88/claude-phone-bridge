"""Sync registry — single read path for all per-collection sync metadata.

Backed by the PB `sync_config` table (one row per synced collection).
Other modules (runner, reconcile, pb_tools) MUST go through this loader
instead of caching their own dicts.

Cache: in-process 60s TTL so per-tool-call lookups don't hammer PB. The
cache is module-level, shared across import sites within one process.
`invalidate()` clears it (called by the REST handlers that mutate
sync_config).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from notion_sync.pb_api import PBClient


@dataclass(frozen=True)
class SyncTarget:
    """One row of sync_config, projected into a typed shape."""
    id: str
    collection: str
    notion_db_id: str
    enabled: bool
    auto_sync: bool
    title_field: str
    date_field: str
    field_map_overrides: dict[str, str]
    last_synced_at: str
    last_sync_summary: str

    @property
    def overrides_inverse(self) -> dict[str, str]:
        return {v: k for k, v in self.field_map_overrides.items()}


_CACHE_TTL_SECONDS = 60.0
_cache: tuple[float, list[SyncTarget]] | None = None


def _row_to_target(row: dict) -> SyncTarget:
    return SyncTarget(
        id=row["id"],
        collection=row["collection"],
        notion_db_id=row.get("notion_db_id") or "",
        enabled=bool(row.get("enabled")),
        auto_sync=bool(row.get("auto_sync")),
        title_field=row.get("title_field") or "",
        date_field=row.get("date_field") or "",
        field_map_overrides=row.get("field_map_overrides") or {},
        last_synced_at=row.get("last_synced_at") or "",
        last_sync_summary=row.get("last_sync_summary") or "",
    )


def load_all(pb: PBClient | None = None, *, fresh: bool = False) -> list[SyncTarget]:
    global _cache
    now = time.monotonic()
    if not fresh and _cache and (now - _cache[0]) < _CACHE_TTL_SECONDS:
        return list(_cache[1])
    pb = pb or PBClient()
    rows = pb.list_records("sync_config", sort="")
    targets = [_row_to_target(r) for r in rows]
    _cache = (now, targets)
    return list(targets)


def load_enabled(pb: PBClient | None = None, *, fresh: bool = False) -> list[SyncTarget]:
    return [t for t in load_all(pb, fresh=fresh) if t.enabled]


def get(collection: str, pb: PBClient | None = None,
        *, fresh: bool = False) -> SyncTarget | None:
    for t in load_all(pb, fresh=fresh):
        if t.collection == collection:
            return t
    return None


def collections_with_auto_sync(pb: PBClient | None = None,
                                *, fresh: bool = False) -> set[str]:
    return {t.collection for t in load_enabled(pb, fresh=fresh) if t.auto_sync}


def invalidate() -> None:
    global _cache
    _cache = None
