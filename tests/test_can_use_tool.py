"""Tests for app.agent.permission.can_use_tool — the SDK permission gate.

Covers the four branches in the docstring of permission.py:

1. Fast-path: Bash + localhost PocketBase curl → Allow
2. AUTO_ALLOW whitelist → Allow
3. state.auto_approve YOLO → Allow (with broadcast)
4. Normal gate: broadcast permission_request + push, await future, timeout → Deny

Plus a sanity test that AUTO_ALLOW isn't accidentally emptied.

No prod code is touched — only monkeypatches on imported names inside
`app.agent.permission` (broadcast, push.send_to_all, asyncio.wait_for,
settings.pocketbase_url).
"""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from app.agent import permission as perm
from app.state import state


@pytest.fixture(autouse=True)
def _reset_state():
    # Each test starts clean — no leftover pending futures or auto_approve flag.
    state.pending.clear()
    state.pending_meta.clear()
    prev_auto = state.auto_approve
    state.auto_approve = False
    yield
    state.pending.clear()
    state.pending_meta.clear()
    state.auto_approve = prev_auto


@pytest.mark.asyncio
async def test_pb_curl_fast_path_allows(monkeypatch):
    """Bash + curl + 127.0.0.1:8090 → Allow without prompting."""
    monkeypatch.setattr(perm.settings, "pocketbase_url",
                        "http://127.0.0.1:8090", raising=True)
    result = await perm.can_use_tool(
        "Bash",
        {"command": "curl -s http://127.0.0.1:8090/api/collections"},
        None,
    )
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_pb_curl_localhost_alias_allows(monkeypatch):
    """Same fast-path but via the `localhost:8090` alias."""
    monkeypatch.setattr(perm.settings, "pocketbase_url",
                        "http://127.0.0.1:8090", raising=True)
    result = await perm.can_use_tool(
        "Bash",
        {"command": "curl -X POST http://localhost:8090/api/health"},
        None,
    )
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_auto_allow_tool_passes_without_prompt():
    """Any tool in AUTO_ALLOW returns Allow immediately, no broadcast needed."""
    tool = next(iter(perm.AUTO_ALLOW))
    result = await perm.can_use_tool(tool, {}, None)
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_auto_approve_global_flag_allows_and_broadcasts(monkeypatch):
    """state.auto_approve=True → Allow, plus a system msg announcing auto-approval."""
    state.auto_approve = True
    sent: list[dict] = []

    async def fake_broadcast(msg):
        sent.append(msg)

    monkeypatch.setattr(perm, "broadcast", fake_broadcast, raising=True)
    # Edit tool isn't in AUTO_ALLOW and isn't the PB fast-path.
    result = await perm.can_use_tool("Edit",
                                     {"file_path": "/tmp/x", "new_text": "y"},
                                     None)
    assert isinstance(result, PermissionResultAllow)
    assert any("auto-approved" in m.get("msg", "") for m in sent), sent


@pytest.mark.asyncio
async def test_normal_gate_broadcasts_request_and_times_out(monkeypatch):
    """No fast-path / no AUTO_ALLOW / no YOLO → broadcast permission_request,
    push, then time out → Deny."""
    sent: list[dict] = []

    async def fake_broadcast(msg):
        sent.append(msg)

    def fake_push_send(*args, **kwargs):
        return None

    async def fake_wait_for(_fut, timeout):  # noqa: ARG001
        raise asyncio.TimeoutError

    monkeypatch.setattr(perm, "broadcast", fake_broadcast, raising=True)
    monkeypatch.setattr(perm.push, "send_to_all", fake_push_send, raising=True)
    monkeypatch.setattr(perm.asyncio, "wait_for", fake_wait_for, raising=True)

    result = await perm.can_use_tool("Edit",
                                     {"file_path": "/tmp/x"}, None)

    assert isinstance(result, PermissionResultDeny)
    assert any(m.get("type") == "permission_request" for m in sent), sent
    # finally: clause should clean up pending state.
    assert state.pending == {}
    assert state.pending_meta == {}


@pytest.mark.asyncio
async def test_normal_gate_allow_decision(monkeypatch):
    """The pending future resolves to 'allow' → Allow."""
    captured_cb_id: dict[str, str] = {}

    async def fake_broadcast(msg):
        if msg.get("type") == "permission_request":
            captured_cb_id["id"] = msg["id"]

    def fake_push_send(*args, **kwargs):
        return None

    real_wait_for = asyncio.wait_for

    async def fake_wait_for(fut, timeout):  # noqa: ARG001
        # Resolve the future immediately as the user clicking "Allow".
        cb_id = captured_cb_id.get("id")
        assert cb_id is not None, "permission_request never broadcast"
        fut = state.pending[cb_id]
        fut.set_result("allow")
        return await real_wait_for(fut, timeout=1)

    monkeypatch.setattr(perm, "broadcast", fake_broadcast, raising=True)
    monkeypatch.setattr(perm.push, "send_to_all", fake_push_send, raising=True)
    monkeypatch.setattr(perm.asyncio, "wait_for", fake_wait_for, raising=True)

    result = await perm.can_use_tool("Edit", {"file_path": "/tmp/x"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert state.pending == {}
    assert state.pending_meta == {}


def test_auto_allow_set_non_empty():
    """Sanity check — losing AUTO_ALLOW silently would force every read-only
    tool through the phone prompt path, which would be miserable."""
    assert len(perm.AUTO_ALLOW) > 0
    assert "Read" in perm.AUTO_ALLOW
