"""Coverage for apply_pending_decisions race + 4 decision paths.

Phase 5 Task 3. Use Notion race fix MUST be characterized by a test
first to lock in correct behavior.

Bug: current code reads Notion's last_edited_time WITHOUT first
patching Notion, then writes that timestamp into PB along with the
notion_snap. Next sync run sees PB.last_synced_at advance but
Notion.last_synced_at unchanged -> false conflict in Sync Activity.

Fix: PATCH Notion's last_synced_at first (advances its last_edited_time),
then read that new last_edited_time, then write PB. Both sides end
up time-synced.
"""
from unittest.mock import MagicMock

from notion_sync.runner import _apply_one_decision


def _make_row(decision: str, *, pb_id: str = "pb1", notion_id: str = "n1",
              notion_snap: str = '{"name": "alpha"}',
              pb_snap: str = '{"id": "pb1", "name": "alpha"}') -> dict:
    """Construct a Sync Activity row with the given decision + snapshots."""
    return {
        "id": "sa_row_id",
        "properties": {
            "decision":  {"select": {"name": decision}},
            "pb_id":     {"rich_text": [{"plain_text": pb_id}]},
            "notion_id": {"rich_text": [{"plain_text": notion_id}]},
            "notion_snapshot": {"rich_text": [{"plain_text": notion_snap}]},
            "pb_snapshot":     {"rich_text": [{"plain_text": pb_snap}]},
        },
    }


def test_use_notion_patches_notion_before_writing_pb():
    """The race fix: nc.update_page must run BEFORE pb.update_record,
    AND pb receives the updated last_edited_time from update_page's response."""
    pb = MagicMock()
    nc = MagicMock()
    nc.update_page.return_value = {
        "id": "n1",
        "last_edited_time": "2026-06-09T10:00:00.000Z",
    }
    row = _make_row("Use Notion", notion_snap='{"name": "alpha"}')

    _apply_one_decision(
        row, pb=pb, nc=nc, collection="trips",
        field_types={}, overrides={}, overrides_inv={},
        title_field="name", notion_schema={},
    )

    # update_page called with last_synced_at property
    assert nc.update_page.called, "update_page must be called to patch Notion"
    update_args = nc.update_page.call_args
    # check properties kwarg or 2nd positional
    props = update_args.kwargs.get("properties") or (
        update_args.args[1] if len(update_args.args) > 1 else {})
    assert "last_synced_at" in props, \
        f"update_page must set last_synced_at, got {props}"

    # pb.update_record received the new last_edited_time
    assert pb.update_record.called
    pb_call = pb.update_record.call_args
    if len(pb_call.args) >= 3:
        data = pb_call.args[2]
    else:
        data = pb_call.kwargs.get("data") or pb_call.kwargs
    assert data.get("notion_last_edited") == "2026-06-09T10:00:00.000Z", \
        f"pb.update_record should use new last_edited_time, got {data}"


def test_use_pb_writes_both_sides(monkeypatch):
    """Use PB pushes PB snapshot back to Notion + records last_synced_at."""
    pb = MagicMock()
    nc = MagicMock()
    nc.update_page.return_value = {"id": "n1", "last_edited_time": "2026-06-09T11:00:00.000Z"}

    # Stub the transform so we don't need a full schema setup
    from notion_sync import runner as runner_mod
    monkeypatch.setattr(runner_mod, "pb_record_to_notion_props",
                        lambda *a, **kw: {"Title": {"title": [{"text": {"content": "alpha"}}]}})
    # Stub icon_for to avoid PB lookup
    monkeypatch.setattr(runner_mod, "icon_for", lambda *a, **kw: None)

    row = _make_row("Use PB", pb_snap='{"id": "pb1", "name": "alpha"}')

    _apply_one_decision(
        row, pb=pb, nc=nc, collection="trips",
        field_types={"name": "text"},
        overrides={}, overrides_inv={},
        title_field="name",
        notion_schema={"Title": {"type": "title"}},
    )

    assert nc.update_page.called
    assert pb.update_record.called


def test_delete_both_calls_both_sides_idempotently():
    pb = MagicMock()
    nc = MagicMock()
    pb.delete_record.side_effect = Exception("already gone")  # tolerated
    row = _make_row("Delete both")

    # Should NOT raise even though delete_record errors
    _apply_one_decision(
        row, pb=pb, nc=nc, collection="trips",
        field_types={}, overrides={}, overrides_inv={},
        title_field="name", notion_schema={},
    )

    pb.delete_record.assert_called_once_with("trips", "pb1")
    nc.update_page.assert_called_once_with("n1", archived=True)


def test_keep_both_is_a_noop():
    pb = MagicMock()
    nc = MagicMock()
    row = _make_row("Keep both")

    _apply_one_decision(
        row, pb=pb, nc=nc, collection="trips",
        field_types={}, overrides={}, overrides_inv={},
        title_field="name", notion_schema={},
    )

    assert not pb.update_record.called
    assert not pb.delete_record.called
    assert not nc.update_page.called
