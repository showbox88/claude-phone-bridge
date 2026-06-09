"""Tests for app.agent.manager.SessionManager.

Covers: get_or_create idempotence; concurrent get_or_create for distinct
sids (no cross-talk); recreate holds turn_lock; destroy cancels in-flight
task + disconnects client; shutdown cleans all.
"""
import asyncio
from pathlib import Path

import pytest

from app.agent.manager import SessionManager
from tests.fakes.sdk_client import FakeClient


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    # Patch the SDK boundary + make_options to skip the real Claude binary.
    import claude_agent_sdk as _sdk
    monkeypatch.setattr(_sdk, "ClaudeSDKClient", FakeClient, raising=True)
    import app.agent.manager as _mgr
    # Patch the local reference inside _connect's closure-target
    monkeypatch.setattr(_mgr, "make_options", lambda agent: {"_agent": agent},
                        raising=False)
    # Also patch the from-import in case _connect imports lazily after monkeypatch
    import app.agent.options as _opts
    monkeypatch.setattr(_opts, "make_options",
                        lambda agent: {"_agent": agent}, raising=True)
    FakeClient.reset()
    yield


@pytest.mark.asyncio
async def test_get_or_create_constructs_one_agent_per_sid():
    mgr = SessionManager()
    a1 = await mgr.get_or_create("sid-A", cwd=Path("/"), mode="code")
    a2 = await mgr.get_or_create("sid-A", cwd=Path("/"), mode="code")
    assert a1 is a2
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].connected is True


@pytest.mark.asyncio
async def test_two_sessions_independent():
    mgr = SessionManager()
    a = await mgr.get_or_create("sid-A", cwd=Path("/"), mode="code", model="opus")
    b = await mgr.get_or_create("sid-B", cwd=Path("/tmp"), mode="chat", model="sonnet")
    assert a.client is not b.client
    assert a.mode == "code" and b.mode == "chat"
    assert a.model == "opus" and b.model == "sonnet"
    assert set(mgr.active_ids()) == {"sid-A", "sid-B"}


@pytest.mark.asyncio
async def test_recreate_waits_for_in_flight_turn():
    mgr = SessionManager()
    a = await mgr.get_or_create("sid-A", cwd=Path("/"))
    # Acquire turn_lock externally to simulate an in-flight turn
    await a.turn_lock.acquire()

    recreate_done = asyncio.Event()

    async def do_recreate():
        await mgr.recreate("sid-A", model="haiku")
        recreate_done.set()

    task = asyncio.create_task(do_recreate())
    # recreate should be blocked on turn_lock — give it 50ms to prove it
    await asyncio.sleep(0.05)
    assert not recreate_done.is_set()
    assert FakeClient.instances[0].disconnect_called is False

    a.turn_lock.release()
    await asyncio.wait_for(task, timeout=1.0)

    assert recreate_done.is_set()
    assert FakeClient.instances[0].disconnect_called is True
    assert a.model == "haiku"
    # second FakeClient was constructed
    assert len(FakeClient.instances) == 2


@pytest.mark.asyncio
async def test_destroy_cancels_in_flight_task():
    mgr = SessionManager()
    a = await mgr.get_or_create("sid-A", cwd=Path("/"))

    async def long_running():
        await asyncio.sleep(60)

    a.current_turn_task = asyncio.create_task(long_running())
    await asyncio.sleep(0)  # let task start

    await mgr.destroy("sid-A")
    assert a.current_turn_task.cancelled()
    assert mgr.get("sid-A") is None


@pytest.mark.asyncio
async def test_shutdown_clears_all():
    mgr = SessionManager()
    await mgr.get_or_create("sid-A", cwd=Path("/"))
    await mgr.get_or_create("sid-B", cwd=Path("/"))
    await mgr.shutdown()
    assert mgr.active_ids() == []
    assert all(c.disconnect_called for c in FakeClient.instances)


@pytest.mark.asyncio
async def test_recreate_unknown_session_raises():
    mgr = SessionManager()
    with pytest.raises(KeyError):
        await mgr.recreate("nope", model="opus")
