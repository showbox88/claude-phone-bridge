"""db.py CRUD coverage.

Phase 6a Task 2. Tests session/message/turn lifecycle on an isolated
sqlite DB (tmp_path) so we don't touch production bridge.db.
"""
from __future__ import annotations

import pytest

import db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db.init(tmp_path / "test_bridge.db")
    yield


def test_create_and_get_session():
    sid = db.create_session(cwd="proj/foo", title="hello",
                            mode="code", model="opus")
    row = db.get_session(sid)
    assert row is not None
    assert row["id"] == sid
    assert row["title"] == "hello"
    assert row["cwd"] == "proj/foo"
    assert row["mode"] == "code"
    assert row["model"] == "opus"
    assert row["messages"] == []


def test_get_session_missing_returns_none():
    assert db.get_session("does-not-exist") is None


def test_append_message_then_get_session_has_it():
    sid = db.create_session(cwd="x")
    msg_seq = db.append_message(sid, "user", {"text": "hi"})
    assert isinstance(msg_seq, int)
    row = db.get_session(sid)
    assert len(row["messages"]) == 1
    assert row["messages"][0]["role"] == "user"
    assert row["messages"][0]["content"] == {"text": "hi"}


def test_update_session_changes_title_and_model():
    sid = db.create_session(cwd="x", title="old")
    db.update_session(sid, title="new", model="haiku")
    row = db.get_session(sid)
    assert row["title"] == "new"
    assert row["model"] == "haiku"


def test_list_sessions_returns_newest_first():
    sid1 = db.create_session(cwd="x", title="first")
    sid2 = db.create_session(cwd="y", title="second")
    sid3 = db.create_session(cwd="z", title="third")
    rows = db.list_sessions()
    ids = [r["id"] for r in rows]
    assert ids[0] == sid3
    assert sid1 in ids and sid2 in ids


def test_search_sessions_matches_title():
    db.create_session(cwd="x", title="alpha task")
    db.create_session(cwd="x", title="beta task")
    db.create_session(cwd="x", title="unrelated")
    rows = db.search_sessions("alpha")
    titles = [r["title"] for r in rows]
    assert "alpha task" in titles
    assert "unrelated" not in titles


def test_append_turn_updates_usage():
    sid = db.create_session(cwd="x")
    db.append_turn(sid, model="opus", mode="code",
                   duration_ms=1000, num_turns=1,
                   input_tokens=100, output_tokens=200,
                   cache_read_tokens=0, cache_create_tokens=0,
                   cost_usd=0.05)
    summary = db.usage_summary()
    # usage_summary() returns {"total": {...}, "today": {...}, "month": {...},
    #                          "by_model": [...], "by_day": [...]}
    total = summary["total"]
    assert total["turns"] == 1
    assert total["in_tok"] == 100
    assert total["out_tok"] == 200
    assert total["cost"] == pytest.approx(0.05)
    by_model = summary["by_model"]
    opus_rows = [r for r in by_model if r["model"] == "opus"]
    assert opus_rows, f"opus not in by_model: {by_model}"
    assert opus_rows[0]["cost"] == pytest.approx(0.05)


def test_delete_session_removes_it():
    sid = db.create_session(cwd="x")
    assert db.get_session(sid) is not None
    db.delete_session(sid)
    assert db.get_session(sid) is None


def test_latest_session_id_filters_by_mode():
    chat_sid = db.create_session(cwd="x", mode="chat")
    code_sid = db.create_session(cwd="y", mode="code")
    assert db.latest_session_id() == code_sid
    assert db.latest_session_id(mode="chat") == chat_sid
    assert db.latest_session_id(mode="code") == code_sid


def test_get_setting_default_returned_for_missing_key():
    assert db.get_setting("never-set", default="fallback") == "fallback"


def test_set_then_get_setting_roundtrips():
    db.set_setting("foo", {"nested": [1, 2, 3]})
    assert db.get_setting("foo") == {"nested": [1, 2, 3]}
