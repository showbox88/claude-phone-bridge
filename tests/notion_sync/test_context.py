"""SyncContext dataclass smoke tests."""
import pytest

from notion_sync.context import SyncContext, make_context


def test_make_context_computes_overrides_inv():
    ctx = make_context(
        collection="days",
        field_types={"start": "Date"},
        overrides={"start": "Start"},
        title_field="title",
        notion_schema={"Start": {"type": "date"}},
    )
    assert ctx.overrides_inv == {"Start": "start"}


def test_context_is_frozen():
    ctx = make_context(
        collection="trips", field_types={}, overrides={},
        title_field="name", notion_schema={},
    )
    with pytest.raises(Exception):
        ctx.collection = "days"


def test_context_defaults_relation_to_none():
    ctx = make_context(
        collection="stops", field_types={}, overrides={},
        title_field="title", notion_schema={},
    )
    assert ctx.relation_lookup is None
    assert ctx.relation_targets is None
