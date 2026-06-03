"""Tests for changeset categorization. Every branch covered."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.changeset import (
    NoChange,
    PbOnlyChange,
    NotionOnlyChange,
    BothChanged,
    PbNew,
    NotionNew,
    NotionVanished,
    PbVanished,
    categorize,
)


def _pb(id_, *, notion_id="", updated="2026-06-01 00:00:00.000Z",
        notion_last_edited=""):
    return {"id": id_, "notion_id": notion_id, "updated": updated,
            "notion_last_edited": notion_last_edited}


def _notion(id_, *, pb_id="", last_edited_time="2026-06-01T00:00:00.000Z"):
    rt = [{"plain_text": pb_id}] if pb_id else []
    return {"id": id_, "last_edited_time": last_edited_time,
            "properties": {"pb_id": {"type": "rich_text", "rich_text": rt}}}


def test_no_change_when_neither_side_moved():
    last = "2026-06-02 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-01 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-01T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert len(actions) == 1
    assert isinstance(actions[0], NoChange)


def test_pb_only_change():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-02 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-01T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert isinstance(actions[0], PbOnlyChange)
    assert actions[0].pb_row["id"] == "p1"
    assert actions[0].notion_id == "n1"


def test_notion_only_change():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-01 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-02T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert isinstance(actions[0], NotionOnlyChange)


def test_both_changed():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-02 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-02T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert isinstance(actions[0], BothChanged)


def test_pb_new_unlinked():
    pb_rows = [_pb("p2")]
    notion_rows = []
    actions = categorize(pb_rows, notion_rows, last_synced_at="2026-06-01 00:00:00.000Z")
    assert isinstance(actions[0], PbNew)


def test_notion_new_unlinked():
    pb_rows = []
    notion_rows = [_notion("n2")]
    actions = categorize(pb_rows, notion_rows, last_synced_at="2026-06-01 00:00:00.000Z")
    assert isinstance(actions[0], NotionNew)


def test_notion_vanished_pb_thinks_linked():
    pb_rows = [_pb("p1", notion_id="n_gone")]
    notion_rows = []
    actions = categorize(pb_rows, notion_rows, last_synced_at="2026-06-01 00:00:00.000Z")
    assert isinstance(actions[0], NotionVanished)


def test_pb_vanished_notion_thinks_linked():
    pb_rows = []
    notion_rows = [_notion("n1", pb_id="p_gone")]
    actions = categorize(pb_rows, notion_rows, last_synced_at="2026-06-01 00:00:00.000Z")
    assert isinstance(actions[0], PbVanished)


def test_mixed_set():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [
        _pb("p1", notion_id="n1",
            updated="2026-06-02 00:00:00.000Z",
            notion_last_edited="2026-06-01T00:00:00.000Z"),
        _pb("p2"),
        _pb("p3", notion_id="n_gone"),
    ]
    notion_rows = [
        _notion("n1", pb_id="p1",
                last_edited_time="2026-06-01T00:00:00.000Z"),
        _notion("n2"),
        _notion("n3", pb_id="p_gone"),
    ]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    kinds = sorted(type(a).__name__ for a in actions)
    assert kinds == sorted([
        "PbOnlyChange", "PbNew", "NotionVanished",
        "NotionNew", "PbVanished",
    ])


def test_iso_t_separator_normalized():
    last = "2026-06-01 00:00:00.000Z"
    pb_rows = [_pb("p1", notion_id="n1",
                    updated="2026-06-02 00:00:00.000Z",
                    notion_last_edited="2026-06-01T00:00:00.000Z")]
    notion_rows = [_notion("n1", pb_id="p1",
                            last_edited_time="2026-06-01T00:00:00.000Z")]
    actions = categorize(pb_rows, notion_rows, last_synced_at=last)
    assert isinstance(actions[0], PbOnlyChange)
