"""Unit tests for the sync registry loader."""
import notion_sync.config as cfg


class FakePB:
    def __init__(self, rows): self.rows = rows
    def list_records(self, *_, **__): return list(self.rows)


def test_load_all_projects_rows():
    pb = FakePB([{
        "id": "r1", "collection": "trips",
        "notion_db_id": "db1", "enabled": True, "auto_sync": True,
        "title_field": "title", "date_field": "date_start",
        "field_map_overrides": {"foo": "Foo"},
        "last_synced_at": "2026-06-04 03:00:00.000Z",
        "last_sync_summary": "",
    }])
    cfg.invalidate()
    targets = cfg.load_all(pb, fresh=True)
    assert len(targets) == 1
    t = targets[0]
    assert t.collection == "trips"
    assert t.title_field == "title"
    assert t.overrides_inverse == {"Foo": "foo"}


def test_collections_with_auto_sync_filters_correctly():
    pb = FakePB([
        {"id": "1", "collection": "trips",   "enabled": True,  "auto_sync": True,  "title_field": "title"},
        {"id": "2", "collection": "plans",   "enabled": True,  "auto_sync": False, "title_field": "title"},
        {"id": "3", "collection": "contacts","enabled": False, "auto_sync": True,  "title_field": "name"},
    ])
    cfg.invalidate()
    assert cfg.collections_with_auto_sync(pb, fresh=True) == {"trips"}


def test_cache_returns_same_list_within_ttl():
    pb = FakePB([{"id": "1", "collection": "trips", "enabled": True,
                   "auto_sync": True, "title_field": "title"}])
    cfg.invalidate()
    a = cfg.load_all(pb)
    pb.rows = []                               # mutate underlying source
    b = cfg.load_all(pb)                       # still cached
    assert a[0].collection == b[0].collection


def test_invalidate_clears_cache():
    pb = FakePB([{"id": "1", "collection": "trips", "enabled": True,
                   "auto_sync": True, "title_field": "title"}])
    cfg.invalidate()
    _ = cfg.load_all(pb)
    pb.rows = []
    cfg.invalidate()
    assert cfg.load_all(pb) == []


def test_get_returns_none_for_unknown():
    pb = FakePB([])
    cfg.invalidate()
    assert cfg.get("nope", pb, fresh=True) is None
