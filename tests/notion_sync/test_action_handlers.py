"""_ACTION_ID_GETTERS dispatch + _action_ids coverage.

Task 2 of Phase 5 refactor: ensures the dict-of-lambdas dispatch table
covers every Action class declared in notion_sync.changeset and that
_action_ids returns the same (pb_id, notion_id) tuples the legacy
isinstance chain returned.
"""
from notion_sync.changeset import (
    BothChanged,
    NoChange,
    NotionNew,
    NotionOnlyChange,
    NotionVanished,
    PbNew,
    PbOnlyChange,
    PbVanished,
)
from notion_sync.runner import _ACTION_ID_GETTERS, _action_ids


def test_every_action_class_in_table():
    """No future Action class accidentally slips through ID extraction."""
    expected = {
        NoChange, PbOnlyChange, NotionOnlyChange, BothChanged,
        PbNew, NotionNew, NotionVanished, PbVanished,
    }
    assert set(_ACTION_ID_GETTERS.keys()) == expected


def test_action_ids_unknown_returns_none_tuple():
    class FakeAction:
        pass

    assert _action_ids(FakeAction()) == (None, None)


def test_action_ids_no_change():
    a = NoChange(pb_id="pb1", notion_id="n1")
    assert _action_ids(a) == ("pb1", "n1")


def test_action_ids_pb_only_change():
    a = PbOnlyChange(pb_row={"id": "pb1"}, notion_id="n1")
    assert _action_ids(a) == ("pb1", "n1")


def test_action_ids_notion_only_change():
    a = NotionOnlyChange(notion_page={"id": "n1"}, pb_id="pb1")
    assert _action_ids(a) == ("pb1", "n1")


def test_action_ids_both_changed():
    a = BothChanged(pb_row={"id": "pb1"}, notion_page={"id": "n1"})
    assert _action_ids(a) == ("pb1", "n1")


def test_action_ids_pb_new():
    a = PbNew(pb_row={"id": "pb1"})
    assert _action_ids(a) == ("pb1", None)


def test_action_ids_notion_new():
    a = NotionNew(notion_page={"id": "n1"})
    assert _action_ids(a) == (None, "n1")


def test_action_ids_notion_vanished_with_notion_id():
    a = NotionVanished(pb_row={"id": "pb1", "notion_id": "n1"})
    assert _action_ids(a) == ("pb1", "n1")


def test_action_ids_notion_vanished_empty_notion_id():
    # PB row carries empty notion_id — should normalize to None, not ""
    a = NotionVanished(pb_row={"id": "pb1", "notion_id": ""})
    assert _action_ids(a) == ("pb1", None)


def test_action_ids_pb_vanished_with_pb_id_property():
    notion_page = {
        "id": "n1",
        "properties": {
            "pb_id": {"rich_text": [{"plain_text": "pb1"}]},
        },
    }
    a = PbVanished(notion_page=notion_page)
    assert _action_ids(a) == ("pb1", "n1")


def test_action_ids_pb_vanished_missing_pb_id_property():
    # PbVanished with no pb_id in notion properties → pb side is None
    notion_page = {"id": "n1", "properties": {}}
    a = PbVanished(notion_page=notion_page)
    assert _action_ids(a) == (None, "n1")


def test_action_ids_handles_malformed_action_gracefully():
    """If an Action's payload is missing expected keys, _action_ids
    returns (None, None) rather than raising — protects the sync loop."""
    # PbOnlyChange constructed with a pb_row that has no 'id' key
    a = PbOnlyChange(pb_row={}, notion_id="n1")
    assert _action_ids(a) == (None, None)
