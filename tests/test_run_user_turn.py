"""run_user_turn end-to-end via FakeClient.

Phase 6a Task 5. Tests app/agent/turn.py — the per-turn pipeline that
takes a user message, queries the SDK, streams responses, and writes
db rows. Uses Phase 3's tests/fakes/sdk_client.py:FakeClient.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import db
from app.agent.agent import ClaudeAgent
from app.agent.turn import run_user_turn
from tests.fakes.sdk_client import FakeClient


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db.init(tmp_path / "test_bridge.db")
    yield


@pytest.fixture
def agent_with_client():
    sid = db.create_session(cwd="x", mode="code", model="opus")
    agent = ClaudeAgent(
        session_id=sid,
        cwd=Path("x"),
        mode="code",
        model="opus",
        sdk_session_id=None,
    )
    agent.client = FakeClient()
    return agent


@pytest.mark.asyncio
async def test_assistant_text_message_persists(agent_with_client, monkeypatch):
    agent = agent_with_client
    agent.client.queue_assistant_text("hi from claude")
    agent.client.queue_result(cost_usd=0.001, input_tokens=10, output_tokens=20)

    broadcast_mock = AsyncMock()
    monkeypatch.setattr("app.agent.turn.broadcast_to_agent", broadcast_mock)

    await run_user_turn(agent, "hello", [], [])

    sess = db.get_session(agent.session_id)
    roles = [m["role"] for m in sess["messages"]]
    assert "user" in roles
    assert "assistant_text" in roles


@pytest.mark.asyncio
async def test_turn_done_broadcasts_with_cost(agent_with_client, monkeypatch):
    agent = agent_with_client
    agent.client.queue_result(cost_usd=0.05, input_tokens=100, output_tokens=200)

    broadcast_mock = AsyncMock()
    monkeypatch.setattr("app.agent.turn.broadcast_to_agent", broadcast_mock)

    await run_user_turn(agent, "ping", [], [])

    # broadcast_to_agent is called with (agent, msg); inspect msg
    sent_msgs = [c.args[1] for c in broadcast_mock.call_args_list if len(c.args) >= 2]
    sent_msgs += [c.kwargs.get("msg") for c in broadcast_mock.call_args_list if c.kwargs.get("msg")]
    turn_done = [m for m in sent_msgs if m and m.get("type") == "turn_done"]
    assert turn_done, f"no turn_done in broadcasts: {sent_msgs}"
    assert turn_done[0]["cost_usd"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_no_active_client_broadcasts_error(monkeypatch):
    sid = db.create_session(cwd="x")
    agent = ClaudeAgent(session_id=sid, cwd=Path("x"), mode="code", model="")
    agent.client = None

    broadcast_mock = AsyncMock()
    monkeypatch.setattr("app.agent.turn.broadcast_to_agent", broadcast_mock)

    await run_user_turn(agent, "anything", [], [])

    sent = [c.args[1] for c in broadcast_mock.call_args_list if len(c.args) >= 2]
    errors = [m for m in sent if m and m.get("type") == "error"]
    assert errors


@pytest.mark.asyncio
async def test_auto_titles_session_from_first_message(agent_with_client, monkeypatch):
    agent = agent_with_client
    assert db.get_session(agent.session_id)["title"] == ""
    agent.client.queue_result(cost_usd=0)
    monkeypatch.setattr("app.agent.turn.broadcast_to_agent", AsyncMock())

    await run_user_turn(agent, "find me a coffee shop please", [], [])

    title = db.get_session(agent.session_id)["title"]
    assert title.startswith("find me a coffee"), f"unexpected title: {title!r}"
