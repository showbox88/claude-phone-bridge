# Phase 3 · Session 多实例化 + Notion 鲁棒性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把进程级 `state.client` 全局单例拆成 per-session `ClaudeAgent`，由 `SessionManager` 管理；多个设备/标签可以独立操作不同 session 互不干扰；切 model/cwd 时只重建当前 session 的 client，不影响其它 session 的 in-flight turn。同时给 `notion_api` 加 429/5xx 退避 + token bucket throttle。

**Architecture:**
- `app/agent/agent.py` 新文件：`ClaudeAgent` dataclass，封装单 session 的所有可变状态（client、cwd、mode、model、turn_lock 等）。
- `app/agent/manager.py` 新文件：`SessionManager` 维护 `Dict[sid, ClaudeAgent]`，懒构造，按需 recreate/destroy。
- `app/state.py` 删除 per-session 字段（client / cwd / mode / model / sdk_session_id / turn_lock / current_turn_task / client_tz / session_id）。保留全局字段（cwd_root / websockets / pending / pending_meta / auto_approve）。新增 `ws_sessions: dict[WebSocket, str]` 跟踪 WS→session 绑定。
- `can_use_tool` 通过 `ContextVar[ClaudeAgent]` 拿到 current agent，broadcast 按绑定 WS 集合过滤。
- `notion_sync/notion_api.py:_http` 加 5xx/429 重试 + Retry-After + 把 `_throttle` 升级为 token bucket。

**Tech Stack:** Python `asyncio.ContextVar`, `dataclasses`, `claude_agent_sdk.ClaudeSDKClient`. Tests use stdlib `unittest.mock` + a `FakeClient` for the SDK boundary.

**Branch:** `refactor/phase-3-session-manager` (已创建)
**Parent spec:** [2026-06-06-refactor-roadmap.md](../specs/2026-06-06-refactor-roadmap.md) §Phase 3
**Roadmap 风险标识：** 高（48h staging soak 必须）

---

## File Structure

| Path | Action | 行数估 | 说明 | Risk |
|---|---|---|---|---|
| `app/agent/agent.py` | Create | 70 | `ClaudeAgent` dataclass + `current_agent` ContextVar | ★★ |
| `app/agent/manager.py` | Create | 140 | `SessionManager`：懒构造、recreate-with-lock、destroy、shutdown | ★★★ |
| `app/agent/session.py` | Modify | -50 | 改成 `manager` 的薄包装：保留 `open_session/new_session` 接口给 ws cmd/API 用 | ★★★ |
| `app/agent/options.py` | Modify | +5 | `make_options(agent)` 改读 `agent.*` 而非 `state.*` | ★★ |
| `app/agent/turn.py` | Modify | +10 | `run_user_turn(agent, ...)` 改读 agent；进入时设置 `current_agent` ContextVar | ★★★★ |
| `app/agent/permission.py` | Modify | +5 | `can_use_tool` 用 `current_agent.get()` 拿 agent；broadcast 用 `broadcast_to_agent` | ★★★ |
| `app/ws/handler.py` | Modify | +30 | 每个 WS 绑 sid；user_message 路由到对应 agent | ★★★★★ |
| `app/ws/broadcast.py` | Modify | +30 | 新增 `broadcast_to_agent(agent, msg)` 仅发给绑定到 agent.sid 的 WS | ★★★ |
| `app/state.py` | Modify | -25 | 删 per-session 字段；加 `ws_sessions: dict[WebSocket, str]` | ★★ |
| `app/main.py` | Modify | +3 | lifespan 用 `manager` 构造 + warm 一个 session | ★★ |
| `app/api/meta.py` | Modify | +5 | `/api/health` 拿"warm session"信息改从 manager 读 | ★ |
| `app/api/sessions.py` | Modify | +5 | DELETE 时调 `manager.destroy(sid)` | ★ |
| `notion_sync/notion_api.py` | Modify | +60 | 429/5xx 退避 + `Retry-After` + token bucket throttle | ★★ |
| `tests/test_session_manager.py` | Create | 200 | 并发 2 session、permission 隔离、recreate-mid-turn 安全 | ★★ |
| `tests/test_notion_api_backoff.py` | Create | 80 | 429+`Retry-After` / 5xx 退避 / 4xx 不重试 / token bucket | ★★ |
| `tests/fakes/sdk_client.py` | Create | 60 | `FakeClient` mock for ClaudeSDKClient | ★ |

**Out of scope (留给后续 phase):**
- 前端协议改造（Phase 4）：当前 PWA 每个标签连一个 WS，已经天然支持多 session 并发，不需要前端协议变化
- 跨进程持久化 manager 状态（重启时丢失活跃 agent — 但 db 里的 session metadata 还在，重启后 lazy 重建）
- Agent eviction LRU（先用无界 dict，发现内存问题再加）

---

## Per-Session State 抽离设计

```python
# app/agent/agent.py
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient


@dataclass
class ClaudeAgent:
    """All mutable state that used to live on `state` but actually belongs
    to a single bridge session. One instance per active session_id."""
    session_id: str                                   # bridge session id
    cwd: Path                                         # working directory
    mode: str = "code"                                # 'code' | 'chat'
    model: str = ""                                   # model alias or ""
    client_tz: str = ""                               # client-reported tz
    sdk_session_id: str | None = None                 # Claude SDK's session id (for resume)
    client: "ClaudeSDKClient | None" = None           # active SDK client
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_turn_task: "asyncio.Task | None" = None


# Set inside run_user_turn() before invoking the SDK. can_use_tool
# reads this when it needs to broadcast a permission_request scoped
# to the right session.
current_agent: ContextVar["ClaudeAgent | None"] = ContextVar(
    "current_agent", default=None
)
```

WS binding (added to `app/state.py`):
```python
ws_sessions: dict[WebSocket, str] = field(default_factory=dict)
```

---

## Task 0: Re-baseline + branch sanity

**Files:**
- Restore: BRIDGE_RECORD scaffolding in `app/main.py` + `app/ws/handler.py` (stripped at end of Phase 2)
- Record: `tests/fixtures/phase3_baseline.jsonl`

The Phase 2 driver (`tests/phase2_drive.py`) is still valid — it doesn't touch `user_message` so it doesn't exercise the agent path. Phase 3 changes are agent-internal; the driver should still produce byte-identical traffic (session-id-randomization already normalized).

- [ ] **Step 1: Confirm branch + clean tree**

```bash
git status
git branch --show-current
```
Expected: clean tree, branch `refactor/phase-3-session-manager`.

- [ ] **Step 2: Restore the BRIDGE_RECORD recorder block in `app/main.py`**

After `app = FastAPI(lifespan=lifespan)` add (paste verbatim — same as Phase 2 Task 0 Step 2):

```python
# --- Phase 3 baseline recorder (BRIDGE_RECORD=1) -----
# Stripped at end of Phase 3 (Task 17).
_recorder = None
if os.environ.get("BRIDGE_RECORD"):
    import sys as _RS
    _RS.path.insert(0, str(BRIDGE_ROOT / "tests"))
    from replay import Recorder as _Recorder  # noqa: E402
    _recorder = _Recorder(Path(os.environ.get(
        "BRIDGE_RECORD_PATH",
        "tests/fixtures/phase3_baseline.jsonl")))


@app.middleware("http")
async def _record_http(request: Request, call_next):
    if not _recorder:
        return await call_next(request)
    req_body = await request.body()

    async def _receive():
        return {"type": "http.request", "body": req_body, "more_body": False}

    request._receive = _receive
    response = await call_next(request)
    chunks = []
    async for c in response.body_iterator:
        chunks.append(c)
    resp_body = b"".join(chunks)
    _recorder.http(request.method, request.url.path,
                   str(request.url.query), req_body,
                   response.status_code, resp_body)
    from starlette.responses import Response as _SR
    return _SR(content=resp_body, status_code=response.status_code,
               headers=dict(response.headers),
               media_type=response.media_type)
# --- end Phase 3 baseline recorder -------------------
```

Re-add `Request` to the `from fastapi import FastAPI` line.

- [ ] **Step 3: Restore the WS frame hooks in `app/ws/handler.py`**

Add near the top of `app/ws/handler.py`, before `router = APIRouter()`:

```python
def _recorder():
    # Lazy lookup so server.py's BRIDGE_RECORD-init runs first.
    import server
    return getattr(server, "_recorder", None)
```

In `ws_handler` after `await ws.accept()`:

```python
    await ws.accept()
    rec = _recorder()
    if rec:
        rec.ws_open()
        _orig_send = ws.send_text
        _orig_recv = ws.receive_text

        async def _rec_send(text):
            await _orig_send(text)
            rec.ws_frame("out", text)

        async def _rec_recv():
            text = await _orig_recv()
            rec.ws_frame("in", text)
            return text

        ws.send_text = _rec_send  # type: ignore[method-assign]
        ws.receive_text = _rec_recv  # type: ignore[method-assign]
```

In the finally block:

```python
    finally:
        state.websockets.discard(ws)
        if rec:
            rec.ws_close(None)
        log.info("websocket closed (remaining=%d)", len(state.websockets))
```

- [ ] **Step 4: Parse**

```bash
python -c "import ast; ast.parse(open('app/main.py', encoding='utf-8').read()); ast.parse(open('app/ws/handler.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 5: Deploy + drive baseline**

```powershell
deploy
```

Enable recorder on VM:
```bash
ssh dashboard-server 'sudo mkdir -p /etc/systemd/system/phone-bridge.service.d && sudo tee /etc/systemd/system/phone-bridge.service.d/bridge-record.conf > /dev/null <<EOF
[Service]
Environment="BRIDGE_RECORD=1"
Environment="BRIDGE_RECORD_PATH=/home/dev/phone-bridge/tests/fixtures/phase3_baseline.jsonl"
EOF
sudo systemctl daemon-reload && rm -f /home/dev/phone-bridge/tests/fixtures/phase3_baseline.jsonl && sudo systemctl restart phone-bridge && sleep 2'
```

Drive:
```powershell
$env:BASE = "https://dashboard-server.tail4cfa2.ts.net"
$env:BRIDGE_COOKIE = "bridge_session=<your-cookie>"
python tests/phase2_drive.py
```

Pull:
```bash
scp dashboard-server:/home/dev/phone-bridge/tests/fixtures/phase3_baseline.jsonl tests/fixtures/phase3_baseline.jsonl
wc -l tests/fixtures/phase3_baseline.jsonl
```
Expected: ≥100 records.

Sanity (same code, two consecutive runs should diff = 0):
```bash
ssh dashboard-server 'sudo tee /etc/systemd/system/phone-bridge.service.d/bridge-record.conf > /dev/null <<EOF
[Service]
Environment="BRIDGE_RECORD=1"
Environment="BRIDGE_RECORD_PATH=/home/dev/phone-bridge/tests/fixtures/phase3_stability.jsonl"
EOF
sudo systemctl daemon-reload && sudo systemctl restart phone-bridge && sleep 2'
```

```powershell
python tests/phase2_drive.py
```

```bash
scp dashboard-server:/home/dev/phone-bridge/tests/fixtures/phase3_stability.jsonl /tmp/stab.jsonl
python tests/replay.py diff tests/fixtures/phase3_baseline.jsonl /tmp/stab.jsonl
```
Expected: `OK: <N> records match`.

Disable recorder:
```bash
ssh dashboard-server 'sudo rm /etc/systemd/system/phone-bridge.service.d/bridge-record.conf && sudo systemctl daemon-reload && sudo systemctl restart phone-bridge'
```

- [ ] **Step 6: Commit baseline + restored recorder scaffolding**

```bash
git add app/main.py app/ws/handler.py tests/fixtures/phase3_baseline.jsonl
git commit -m "test(replay): record Phase 3 baseline + restore BRIDGE_RECORD scaffolding"
```

---

## Task 1: `ClaudeAgent` dataclass + `current_agent` ContextVar

**Files:**
- Create: `app/agent/agent.py`

- [ ] **Step 1: Write `app/agent/agent.py`** (use the §Per-Session State 抽离设计 code verbatim)

- [ ] **Step 2: Sanity import**

```bash
python -c "from app.agent.agent import ClaudeAgent, current_agent; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add app/agent/agent.py
git commit -m "refactor(agent): add ClaudeAgent dataclass + current_agent ContextVar"
```

---

## Task 2: `FakeClient` test fixture

**Files:**
- Create: `tests/fakes/__init__.py` (empty)
- Create: `tests/fakes/sdk_client.py`

The real `ClaudeSDKClient.connect()` spawns a Claude subprocess — not friendly for unit tests. A `FakeClient` exposes the same interface (connect/disconnect/query/receive_response) and records calls so tests can assert.

- [ ] **Step 1: Create `tests/fakes/__init__.py`** (empty)

- [ ] **Step 2: Create `tests/fakes/sdk_client.py`**

```python
"""FakeClient: in-memory stand-in for ClaudeSDKClient.

Records every method call so tests can assert ordering, lets tests inject
scripted response streams. Not a perfect mock — covers only what
SessionManager / run_user_turn actually use."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, options: Any = None):
        self.options = options
        self.connected = False
        self.disconnect_called = False
        self.queries: list[Any] = []
        self.scripted_response: list[Any] = []
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_called = True
        self.connected = False

    async def query(self, msg_iter: AsyncIterator[Any]) -> None:
        async for msg in msg_iter:
            self.queries.append(msg)

    async def receive_response(self) -> AsyncIterator[Any]:
        for item in self.scripted_response:
            if isinstance(item, BaseException):
                raise item
            await asyncio.sleep(0)
            yield item

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
```

- [ ] **Step 3: Verify import**

```bash
python -c "from tests.fakes.sdk_client import FakeClient; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add tests/fakes/
git commit -m "test(fakes): add FakeClient stand-in for ClaudeSDKClient"
```

---

## Task 3: `SessionManager` core

**Files:**
- Create: `app/agent/manager.py`

- [ ] **Step 1: Write `app/agent/manager.py`**

```python
"""SessionManager — per-session ClaudeAgent registry.

Lazily constructs a ClaudeAgent for each active session_id. Recreate
(model/cwd switch) holds the agent's turn_lock so it never tears down
an in-flight turn. Destroy disconnects the SDK client cleanly.
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from app.agent.agent import ClaudeAgent

log = logging.getLogger("bridge")


class SessionManager:
    def __init__(self) -> None:
        self._agents: dict[str, ClaudeAgent] = {}

    def get(self, sid: str) -> ClaudeAgent | None:
        return self._agents.get(sid)

    async def get_or_create(self, sid: str, *, cwd: Path,
                            mode: str = "code", model: str = "",
                            sdk_session_id: str | None = None) -> ClaudeAgent:
        """Return existing agent for sid; create + connect if absent."""
        existing = self._agents.get(sid)
        if existing is not None:
            return existing
        agent = ClaudeAgent(
            session_id=sid, cwd=cwd, mode=mode, model=model,
            sdk_session_id=sdk_session_id,
        )
        await self._connect(agent)
        self._agents[sid] = agent
        return agent

    async def _connect(self, agent: ClaudeAgent) -> None:
        # Delayed import to avoid pulling claude_agent_sdk at module load
        # time (it spawns the bundled CLI on first import).
        from claude_agent_sdk import ClaudeSDKClient
        from app.agent.options import make_options

        log.info("agent connect sid=%s mode=%s model=%s cwd=%s",
                 agent.session_id, agent.mode, agent.model or "default",
                 agent.cwd)
        agent.client = ClaudeSDKClient(options=make_options(agent))
        await agent.client.connect()

    async def recreate(self, sid: str, *, cwd: Path | None = None,
                       mode: str | None = None,
                       model: str | None = None,
                       sdk_session_id: str | None = None) -> ClaudeAgent:
        """Tear down + reconnect this session's client without affecting
        others. Waits for any in-flight turn to finish (holds turn_lock)."""
        agent = self._agents.get(sid)
        if agent is None:
            raise KeyError(f"no agent for session {sid}")
        async with agent.turn_lock:
            if agent.client is not None:
                with contextlib.suppress(Exception):
                    await agent.client.disconnect()
                agent.client = None
            if cwd is not None: agent.cwd = cwd
            if mode is not None: agent.mode = mode
            if model is not None: agent.model = model
            if sdk_session_id is not None: agent.sdk_session_id = sdk_session_id
            await self._connect(agent)
        return agent

    async def destroy(self, sid: str) -> None:
        agent = self._agents.pop(sid, None)
        if agent is None:
            return
        if agent.current_turn_task and not agent.current_turn_task.done():
            agent.current_turn_task.cancel()
            with contextlib.suppress(BaseException):
                await agent.current_turn_task
        if agent.client is not None:
            with contextlib.suppress(Exception):
                await agent.client.disconnect()

    async def shutdown(self) -> None:
        sids = list(self._agents.keys())
        for sid in sids:
            await self.destroy(sid)

    def active_ids(self) -> list[str]:
        return list(self._agents.keys())


manager: SessionManager = SessionManager()
```

- [ ] **Step 2: Sanity import (does not exercise `_connect` yet)**

```bash
python -c "from app.agent.manager import SessionManager, manager; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add app/agent/manager.py
git commit -m "refactor(agent): add SessionManager with lazy get_or_create + recreate-under-lock"
```

---

## Task 4: SessionManager unit tests

**Files:**
- Create: `tests/test_session_manager.py`
- Modify: `requirements-dev.txt` (add `pytest-asyncio>=0.23`)

- [ ] **Step 1: Add pytest-asyncio**

```bash
grep -q pytest-asyncio requirements-dev.txt || echo "pytest-asyncio>=0.23" >> requirements-dev.txt
```

- [ ] **Step 2: Write `tests/test_session_manager.py`**

```python
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
```

- [ ] **Step 3: Run tests on VM (local Windows venv may lack bcrypt etc.)**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/pip install pytest-asyncio 2>&1 | tail -2'
```

But wait — tests/fakes is excluded from deploy (the deploy excludes `tests` per `.deploy.json`). Run tests locally if possible; otherwise scp them up:

```bash
ssh dashboard-server 'mkdir -p /home/dev/phone-bridge/tests/fakes'
scp tests/fakes/__init__.py tests/fakes/sdk_client.py tests/test_session_manager.py dashboard-server:/home/dev/phone-bridge/tests/
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/pytest tests/test_session_manager.py -v 2>&1 | tail -15'
```

Expected: 6 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_session_manager.py tests/fakes/ requirements-dev.txt
git commit -m "test(session-manager): 6 tests covering get_or_create / recreate / destroy / shutdown"
```

---

## Task 5: `make_options(agent)` accepts ClaudeAgent

**Files:**
- Modify: `app/agent/options.py`

Currently `make_options(resume_sdk_id)` reads from global `state`. Change signature to `make_options(agent)` reading from `agent.cwd / agent.mode / agent.model / agent.client_tz / agent.sdk_session_id`.

- [ ] **Step 1: Edit `app/agent/options.py:make_options`**

Replace the function body with:

```python
def make_options(agent) -> ClaudeAgentOptions:
    """Build SDK options from a ClaudeAgent. Replaces the old
    `make_options(resume_sdk_id)` signature; reads cwd/mode/model/
    client_tz/sdk_session_id from the agent instead of global state."""
    kwargs: dict[str, Any] = dict(
        cwd=str(agent.cwd),
        can_use_tool=can_use_tool,
    )
    if agent.mode == "chat":
        kwargs["system_prompt"] = CHAT_SYSTEM_PROMPT
        kwargs["allowed_tools"] = list(CHAT_TOOLS)
    else:
        kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code"}
        kwargs["allowed_tools"] = list(AUTO_ALLOW)

    if PB_MCP_SERVER:
        kwargs["mcp_servers"] = {pb_tools.SERVER_NAME: PB_MCP_SERVER}
        kwargs["allowed_tools"] = kwargs["allowed_tools"] + pb_tools.SAFE_TOOL_NAMES
        if isinstance(kwargs["system_prompt"], str):
            kwargs["system_prompt"] = kwargs["system_prompt"] + "\n\n" + pb_tools.PROMPT_HINT
        else:
            kwargs["system_prompt"] = {**kwargs["system_prompt"],
                                       "append": pb_tools.PROMPT_HINT}

    if agent.client_tz:
        tz_note = (
            f"\n\n[runtime] Current user timezone: {agent.client_tz}. "
            f"When a user says relative times like '明天3点' or 'tomorrow 6pm', "
            f"resolve them per the rules in SMARTNOTE_PROMPT.md (Timezone section)."
        )
        sp = kwargs.get("system_prompt")
        if isinstance(sp, str):
            kwargs["system_prompt"] = sp + tz_note
        elif isinstance(sp, dict):
            kwargs["system_prompt"] = {
                **sp,
                "append": (sp.get("append", "") or "") + tz_note,
            }

    if agent.model:
        kwargs["model"] = agent.model
    if agent.sdk_session_id:
        kwargs["resume"] = agent.sdk_session_id
    return ClaudeAgentOptions(**kwargs)
```

Drop `from app.state import state` (only `make_options` used `state.*`; the rest of the file is constants).

- [ ] **Step 2: Parse + import**

```bash
python -c "import ast; ast.parse(open('app/agent/options.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add app/agent/options.py
git commit -m "refactor(agent): make_options(agent) reads from ClaudeAgent instead of state"
```

---

## Task 6: `run_user_turn(agent, ...)` + `current_agent` ContextVar wiring

**Files:**
- Modify: `app/agent/turn.py`
- Modify: `app/ws/broadcast.py` (temp stub of `broadcast_to_agent`)

Note: `broadcast_to_agent` real implementation lands in Task 8. Until then, add a temporary stub that just calls `broadcast(msg)`. This lets Task 6 ship as a discrete commit without breaking the import chain.

- [ ] **Step 1: Add temp stub in `app/ws/broadcast.py`**

After the existing `broadcast` function, append:

```python
async def broadcast_to_agent(agent, msg: dict) -> None:
    """Phase-3-Task-6 stub: forwards to broadcast() for now. Task 8
    replaces with WS-binding-aware routing."""
    await broadcast(msg)
```

- [ ] **Step 2: Edit `app/agent/turn.py`**

Add import near the top:
```python
from app.agent.agent import current_agent
from app.ws.broadcast import broadcast_to_agent  # along with the existing broadcast import
```

Change `_save_msg`:
```python
def _save_msg(agent, role: str, content: dict) -> None:
    db.append_message(agent.session_id, role, content)
```

Replace `run_user_turn` signature + body to use `agent.*` and `broadcast_to_agent`:

```python
async def run_user_turn(
    agent, text: str, images: list[str] | None = None,
    files: list[str] | None = None,
) -> None:
    images = images or []
    files = files or []
    current_agent.set(agent)
    async with agent.turn_lock:
        if agent.client is None:
            await broadcast_to_agent(agent,
                {"type": "error", "msg": "no active session"})
            return
        # auto-title from first user message in this session
        sess = db.get_session(agent.session_id)
        if sess is not None and not sess["title"] and text:
            db.update_session(agent.session_id, title=text.strip()[:40])

        _save_msg(agent, "user",
                  {"text": text, "images": images, "files": files})
        content = _build_user_content(text, images, files)

        async def msg_stream():
            yield {"type": "user",
                   "message": {"role": "user", "content": content},
                   "parent_tool_use_id": None}

        try:
            await agent.client.query(msg_stream())
            async for msg in agent.client.receive_response():
                if isinstance(msg, (AssistantMessage, UserMessage)):
                    for block in getattr(msg, "content", []) or []:
                        ev = _block_to_event(block)
                        if ev is None:
                            continue
                        await broadcast_to_agent(agent, ev)
                        if ev["type"] == "assistant_text":
                            _save_msg(agent, "assistant_text", {"text": ev["text"]})
                        elif ev["type"] == "tool_use":
                            _save_msg(agent, "tool_use", {
                                "id": ev["id"], "tool": ev["tool"],
                                "input": ev["input"],
                            })
                        elif ev["type"] == "tool_result":
                            _save_msg(agent, "tool_result", {
                                "id": ev["id"], "ok": ev["ok"],
                                "content": ev["content"],
                            })
                elif isinstance(msg, ResultMessage):
                    sid_from_sdk = getattr(msg, "session_id", None)
                    if sid_from_sdk:
                        agent.sdk_session_id = sid_from_sdk
                        db.update_session(agent.session_id,
                                          sdk_session_id=sid_from_sdk)
                    cost = getattr(msg, "total_cost_usd", None) or 0.0
                    usage = getattr(msg, "usage", None) or {}
                    in_tok = int(usage.get("input_tokens") or 0)
                    out_tok = int(usage.get("output_tokens") or 0)
                    cache_read = int(usage.get("cache_read_input_tokens") or 0)
                    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
                    duration = int(getattr(msg, "duration_ms", 0) or 0)
                    nturns = int(getattr(msg, "num_turns", 0) or 0)
                    db.append_turn(
                        agent.session_id,
                        model=agent.model, mode=agent.mode,
                        duration_ms=duration, num_turns=nturns,
                        input_tokens=in_tok, output_tokens=out_tok,
                        cache_read_tokens=cache_read,
                        cache_create_tokens=cache_create,
                        cost_usd=float(cost),
                    )
                    await broadcast_to_agent(agent, {
                        "type": "turn_done",
                        "session_id": sid_from_sdk,
                        "cost_usd": cost,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "duration_ms": duration,
                    })
                    break
        except asyncio.CancelledError:
            await broadcast_to_agent(agent,
                {"type": "system", "msg": "turn cancelled"})
            raise
        except Exception as e:
            log.exception("turn failed")
            await broadcast_to_agent(agent,
                {"type": "error", "msg": f"{type(e).__name__}: {e}"})
```

- [ ] **Step 3: Parse**

```bash
python -c "import ast; ast.parse(open('app/agent/turn.py', encoding='utf-8').read()); ast.parse(open('app/ws/broadcast.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add app/agent/turn.py app/ws/broadcast.py
git commit -m "refactor(agent): run_user_turn(agent, ...) + current_agent ContextVar; broadcast_to_agent stub"
```

---

## Task 7: `can_use_tool` reads `current_agent`

**Files:**
- Modify: `app/agent/permission.py`

- [ ] **Step 1: Edit `app/agent/permission.py`**

Add imports near the top:
```python
from app.agent.agent import current_agent
from app.ws.broadcast import broadcast_to_agent
```

Replace `can_use_tool` body:

```python
async def can_use_tool(tool_name: str, tool_input: dict, context):  # noqa: ARG001
    agent = current_agent.get()
    # Fast-path: 打卡 Bash curl to local PocketBase (no phone confirmation)
    if tool_name == "Bash" and settings.pocketbase_url:
        cmd = str(tool_input.get("command", ""))
        if ("127.0.0.1:8090" in cmd or "localhost:8090" in cmd) and \
                ("curl " in cmd or "curl\n" in cmd):
            return PermissionResultAllow(behavior="allow", updated_input=None)
    if tool_name in AUTO_ALLOW:
        return PermissionResultAllow(behavior="allow", updated_input=None)

    # state.auto_approve stays GLOBAL (process-wide YOLO toggle)
    if state.auto_approve:
        if agent is not None:
            await broadcast_to_agent(agent,
                {"type": "system", "msg": f"🚀 auto-approved {tool_name}"})
        else:
            await broadcast({"type": "system",
                             "msg": f"🚀 auto-approved {tool_name}"})
        return PermissionResultAllow(behavior="allow", updated_input=None)

    cb_id = secrets.token_urlsafe(8)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    state.pending[cb_id] = fut
    state.pending_meta[cb_id] = {
        "tool": tool_name, "input": tool_input,
        "session_id": agent.session_id if agent else None,
    }

    perm_msg = {
        "type": "permission_request",
        "id": cb_id,
        "tool": tool_name,
        "input": tool_input,
        "session_id": agent.session_id if agent else None,
    }
    if agent is not None:
        await broadcast_to_agent(agent, perm_msg)
    else:
        await broadcast(perm_msg)

    await asyncio.to_thread(
        push.send_to_all,
        f"🔧 Claude wants to run {tool_name}",
        summarize_input(tool_input)[:180],
        cb_id,
    )

    try:
        decision = await asyncio.wait_for(fut, timeout=600)
    except asyncio.TimeoutError:
        if agent is not None:
            await broadcast_to_agent(agent,
                {"type": "system", "msg": f"{tool_name} timed out, denied"})
            await broadcast_to_agent(agent,
                {"type": "permission_resolved", "id": cb_id, "decision": "timeout"})
        else:
            await broadcast({"type": "system",
                             "msg": f"{tool_name} timed out, denied"})
            await broadcast({"type": "permission_resolved",
                             "id": cb_id, "decision": "timeout"})
        return PermissionResultDeny(behavior="deny",
                                    message="user did not respond in time")
    finally:
        state.pending.pop(cb_id, None)
        state.pending_meta.pop(cb_id, None)

    if decision == "allow":
        return PermissionResultAllow(behavior="allow", updated_input=None)
    return PermissionResultDeny(behavior="deny", message="user rejected via web UI")
```

- [ ] **Step 2: Parse**

```bash
python -c "import ast; ast.parse(open('app/agent/permission.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add app/agent/permission.py
git commit -m "refactor(agent): can_use_tool reads current_agent + scopes broadcasts to it"
```

---

## Task 8: `broadcast_to_agent` real implementation (WS-binding-aware)

**Files:**
- Modify: `app/state.py` — add `ws_sessions: dict[WebSocket, str]`
- Modify: `app/ws/broadcast.py` — replace stub with real implementation

- [ ] **Step 1: Edit `app/state.py` — add `ws_sessions` field**

In `AppState` dataclass, after `websockets` add:
```python
    # Per-WS session binding: which session a given WebSocket is currently
    # "watching". Set on connect (from hello) and on cmd:load_session.
    ws_sessions: "dict[WebSocket, str]" = field(default_factory=dict)
```

(Other state cleanup happens in Task 11.)

- [ ] **Step 2: Edit `app/ws/broadcast.py`**

Replace the temp stub with the real implementation:

```python
"""WebSocket fan-out helpers.

`broadcast(msg)` — to every WS (system-wide events like sessions_changed).
`broadcast_to_agent(agent, msg)` — only to WSs bound to that agent's
session (assistant_text, tool_use, permission_request, turn_done).
"""
from __future__ import annotations

import json

from app.state import state


async def broadcast(msg: dict) -> None:
    payload = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in list(state.websockets):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.websockets.discard(ws)
        state.ws_sessions.pop(ws, None)


async def broadcast_to_agent(agent, msg: dict) -> None:
    """Fan-out only to WSs bound to agent.session_id. Drops the frame
    quietly if no WS is bound (e.g. server-driven turn with no client
    connected — db row still persisted via _save_msg)."""
    sid = agent.session_id
    targets = [ws for ws in list(state.websockets)
               if state.ws_sessions.get(ws) == sid]
    if not targets:
        return
    payload = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in targets:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.websockets.discard(ws)
        state.ws_sessions.pop(ws, None)
```

- [ ] **Step 3: Parse + import**

```bash
python -c "from app.ws.broadcast import broadcast, broadcast_to_agent; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add app/state.py app/ws/broadcast.py
git commit -m "refactor(ws): broadcast_to_agent scopes to ws_sessions binding"
```

---

## Task 9: WS handler — bind sid + route via manager

**Files:**
- Modify: `app/ws/handler.py`

Big change. Replace the file body (keep top docstring + recorder shim from Task 0).

- [ ] **Step 1: Rewrite `app/ws/handler.py`**

```python
"""WebSocket endpoint + message dispatch.

Each WS connection binds to a single session_id (from db.latest_session_id()
on accept, or via cmd:load_session). All session-specific events fan out
only to WSs bound to that session.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import auth as auth_mod
import db

from app.agent.manager import manager
from app.agent.options import AVAILABLE_MODELS
from app.agent.turn import run_user_turn
from app.auth.state import auth_state
from app.persistence.files import _resolve_in_root, _to_rel, uploads_dir
from app.state import state
from app.ws.broadcast import broadcast, broadcast_to_agent

log = logging.getLogger("bridge")
router = APIRouter()


def _recorder():
    import server
    return getattr(server, "_recorder", None)


async def _ensure_agent_for_ws(ws: WebSocket):
    """Pick a default session for this WS and ensure an agent exists.
    Sets state.ws_sessions[ws] = sid. Returns the agent (or None on db error)."""
    sid = db.latest_session_id()
    if not sid:
        from app.agent.session import new_session
        sid = await new_session()
    sess = db.get_session(sid)
    if not sess:
        return None
    cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
    if not str(cwd).startswith(str(state.cwd_root)):
        cwd = state.cwd_root
    agent = await manager.get_or_create(
        sid, cwd=cwd,
        mode=sess.get("mode") or "code",
        model=sess.get("model") or "",
        sdk_session_id=sess.get("sdk_session_id"),
    )
    state.ws_sessions[ws] = sid
    return agent


@router.websocket("/ws")
async def ws_handler(ws: WebSocket):
    if auth_state.is_initialized():
        token = ws.cookies.get(auth_mod.COOKIE_NAME)
        if not token or auth_state.lookup_token(token) is None:
            await ws.close(code=4401)
            return
    await ws.accept()
    rec = _recorder()
    if rec:
        rec.ws_open()
        _orig_send = ws.send_text
        _orig_recv = ws.receive_text

        async def _rec_send(text):
            await _orig_send(text)
            rec.ws_frame("out", text)

        async def _rec_recv():
            text = await _orig_recv()
            rec.ws_frame("in", text)
            return text

        ws.send_text = _rec_send  # type: ignore[method-assign]
        ws.receive_text = _rec_recv  # type: ignore[method-assign]
    state.websockets.add(ws)
    log.info("websocket connected (total=%d)", len(state.websockets))
    try:
        agent = await _ensure_agent_for_ws(ws)
        hello: dict[str, Any] = {
            "type": "hello",
            "cwd": _to_rel(agent.cwd) if agent else "",
            "session_id": agent.session_id if agent else None,
            "auto_approve": state.auto_approve,
        }
        if agent and agent.session_id:
            sess = db.get_session(agent.session_id)
            if sess:
                hello["session"] = {
                    "id": sess["id"], "title": sess["title"],
                    "cwd": sess["cwd"],
                    "mode": sess.get("mode") or "code",
                    "model": sess.get("model") or "",
                    "messages": sess["messages"],
                }
        sid = agent.session_id if agent else None
        hello["pending_perms"] = [
            {"id": cid, "tool": meta.get("tool"), "input": meta.get("input")}
            for cid, meta in state.pending_meta.items()
            if cid in state.pending and not state.pending[cid].done()
            and (meta.get("session_id") in (None, sid))
        ]
        await ws.send_text(json.dumps(hello, ensure_ascii=False))

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "msg": "invalid JSON"}))
                continue
            await handle_ws_message(ws, msg)
    except WebSocketDisconnect:
        pass
    finally:
        state.websockets.discard(ws)
        state.ws_sessions.pop(ws, None)
        if rec:
            rec.ws_close(None)
        log.info("websocket closed (remaining=%d)", len(state.websockets))


def _agent_for_ws(ws: WebSocket):
    sid = state.ws_sessions.get(ws)
    return manager.get(sid) if sid else None


async def handle_ws_message(ws: WebSocket, msg: dict) -> None:
    t = msg.get("type")
    if t == "user_message":
        text = (msg.get("text") or "").strip()
        images = msg.get("images") or []
        files = msg.get("files") or []
        client_tz = (msg.get("client_tz") or "").strip()
        agent = _agent_for_ws(ws)
        if agent is None:
            await ws.send_text(json.dumps(
                {"type": "error", "msg": "no active session"}))
            return
        if client_tz:
            agent.client_tz = client_tz
        if not text and not images and not files:
            return
        await broadcast_to_agent(agent, {
            "type": "user_echo", "text": text,
            "images": images, "files": files,
        })
        agent.current_turn_task = asyncio.create_task(
            run_user_turn(agent, text, images, files))
    elif t == "permission_response":
        cb_id = msg.get("id")
        decision = msg.get("decision")
        fut = state.pending.get(cb_id) if cb_id else None
        if fut and not fut.done():
            fut.set_result(decision)
            meta = state.pending_meta.get(cb_id, {})
            sid_for_msg = meta.get("session_id")
            payload = {"type": "permission_resolved",
                       "id": cb_id, "decision": decision}
            if sid_for_msg:
                ag = manager.get(sid_for_msg)
                if ag is not None:
                    await broadcast_to_agent(ag, payload)
                else:
                    await broadcast(payload)
            else:
                await broadcast(payload)
    elif t == "cmd":
        await handle_cmd(ws, msg)
    elif t == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))


async def handle_cmd(ws: WebSocket, msg: dict) -> None:
    name = msg.get("name")
    if name == "new_session":
        mode = msg.get("mode") if msg.get("mode") in ("code", "chat") else "code"
        from app.agent.session import new_session
        sid = await new_session(cwd_rel=msg.get("cwd"), mode=mode)
        state.ws_sessions[ws] = sid
        agent = manager.get(sid)
        if agent:
            await broadcast_to_agent(agent, {
                "type": "session_loaded",
                "session": _session_payload(sid, agent),
            })
    elif name == "load_session":
        sid = msg.get("id")
        if not sid: return
        sess = db.get_session(sid)
        if not sess:
            await ws.send_text(json.dumps(
                {"type": "error", "msg": f"session not found: {sid}"}))
            return
        cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
        if not str(cwd).startswith(str(state.cwd_root)):
            cwd = state.cwd_root
        agent = await manager.get_or_create(
            sid, cwd=cwd,
            mode=sess.get("mode") or "code",
            model=sess.get("model") or "",
            sdk_session_id=sess.get("sdk_session_id"),
        )
        state.ws_sessions[ws] = sid
        await ws.send_text(json.dumps({
            "type": "session_loaded",
            "session": _session_payload(sid, agent),
        }, ensure_ascii=False))
    elif name == "delete_session":
        sid = msg.get("id")
        if not sid: return
        if db.get_session(sid):
            await manager.destroy(sid)
            db.delete_session(sid)
            sdir = uploads_dir() / sid
            if sdir.is_dir():
                with contextlib.suppress(OSError):
                    shutil.rmtree(sdir)
            for w, bound in list(state.ws_sessions.items()):
                if bound == sid:
                    latest = db.latest_session_id()
                    if latest:
                        latest_sess = db.get_session(latest)
                        if latest_sess:
                            cwd = (state.cwd_root / latest_sess["cwd"]).resolve() if latest_sess["cwd"] else state.cwd_root
                            await manager.get_or_create(
                                latest, cwd=cwd,
                                mode=latest_sess.get("mode") or "code",
                                model=latest_sess.get("model") or "",
                                sdk_session_id=latest_sess.get("sdk_session_id"),
                            )
                            state.ws_sessions[w] = latest
                    else:
                        state.ws_sessions.pop(w, None)
            await broadcast({"type": "session_deleted", "id": sid})
    elif name == "rename_session":
        sid = msg.get("id"); title = msg.get("title")
        if sid and title is not None:
            db.update_session(sid, title=str(title)[:80])
            await broadcast({"type": "session_renamed",
                             "id": sid, "title": title})
    elif name == "switch_workspace":
        new_mode = msg.get("mode")
        if new_mode not in ("code", "chat"): return
        target_sid = db.latest_session_id(mode=new_mode)
        if target_sid:
            sess = db.get_session(target_sid)
            cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
            agent = await manager.get_or_create(
                target_sid, cwd=cwd, mode=sess.get("mode") or "code",
                model=sess.get("model") or "",
                sdk_session_id=sess.get("sdk_session_id"),
            )
            state.ws_sessions[ws] = target_sid
            await ws.send_text(json.dumps({
                "type": "session_loaded",
                "session": _session_payload(target_sid, agent),
            }, ensure_ascii=False))
        else:
            from app.agent.session import new_session
            sid = await new_session(mode=new_mode)
            state.ws_sessions[ws] = sid
    elif name == "set_auto_approve":
        new_val = bool(msg.get("value"))
        if new_val == state.auto_approve: return
        state.auto_approve = new_val
        await broadcast({
            "type": "auto_approve_changed", "value": state.auto_approve,
        })
        await broadcast({
            "type": "system",
            "msg": ("🚀 自动批准已开启 — 后续工具调用不再询问"
                    if state.auto_approve
                    else "🛑 自动批准已关闭 — 恢复逐次询问"),
        })
    elif name == "set_model":
        new_model = msg.get("model") or ""
        if new_model not in {m["id"] for m in AVAILABLE_MODELS}:
            return
        agent = _agent_for_ws(ws)
        if agent is None or new_model == agent.model: return
        db.update_session(agent.session_id, model=new_model)
        await manager.recreate(agent.session_id, model=new_model)
        await broadcast_to_agent(agent, {
            "type": "session_model_changed",
            "id": agent.session_id, "model": new_model,
        })
    elif name == "cwd":
        rel = msg.get("path", "")
        new_cwd = _resolve_in_root(rel)
        if new_cwd is None or not new_cwd.is_dir():
            await ws.send_text(json.dumps(
                {"type": "error", "msg": f"invalid cwd: {rel}"}))
            return
        agent = _agent_for_ws(ws)
        if agent is None: return
        db.update_session(agent.session_id, cwd=_to_rel(new_cwd))
        await manager.recreate(agent.session_id, cwd=new_cwd)
    elif name == "cancel":
        agent = _agent_for_ws(ws)
        if agent is None: return
        task = agent.current_turn_task
        if task and not task.done():
            task.cancel()
        else:
            await broadcast_to_agent(agent,
                {"type": "system", "msg": "nothing to cancel"})


def _session_payload(sid: str, agent) -> dict:
    sess = db.get_session(sid)
    return {
        "id": sid,
        "title": sess.get("title", "") if sess else "",
        "cwd": _to_rel(agent.cwd),
        "mode": agent.mode,
        "model": agent.model,
        "messages": sess["messages"] if sess else [],
    }
```

- [ ] **Step 2: Parse**

```bash
python -c "import ast; ast.parse(open('app/ws/handler.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add app/ws/handler.py
git commit -m "refactor(ws): bind WS to session_id; route all events via manager"
```

---

## Task 10: `app/agent/session.py` becomes a thin wrapper

**Files:**
- Modify: `app/agent/session.py`

- [ ] **Step 1: Replace `app/agent/session.py`**

```python
"""Bridge session lifecycle — thin wrapper around SessionManager.

Kept as a stable module so callers (`app/api/sessions.py`, ws handler,
lifespan) don't have to change import paths. All real work delegates to
`app.agent.manager.manager`.
"""
from __future__ import annotations

import logging

import db

from app.agent.manager import manager
from app.state import state

log = logging.getLogger("bridge")


async def open_session(sid: str):
    """Load session from db + ensure manager has a live agent for it.
    Returns the agent (or None if session not found)."""
    sess = db.get_session(sid)
    if sess is None:
        log.warning("open_session: not found %s", sid)
        return None
    cwd = (state.cwd_root / sess["cwd"]).resolve() if sess["cwd"] else state.cwd_root
    if not str(cwd).startswith(str(state.cwd_root)):
        cwd = state.cwd_root
    return await manager.get_or_create(
        sid, cwd=cwd,
        mode=sess.get("mode") or "code",
        model=sess.get("model") or "",
        sdk_session_id=sess.get("sdk_session_id"),
    )


async def new_session(cwd_rel: str | None = None,
                      mode: str = "code", model: str = "") -> str:
    """Create a new bridge session row + ensure a live agent for it."""
    from app.persistence.files import _resolve_in_root, _to_rel
    target_cwd = state.cwd_root
    if cwd_rel:
        resolved = _resolve_in_root(cwd_rel)
        if resolved and resolved.is_dir():
            target_cwd = resolved
    rel_cwd = _to_rel(target_cwd)
    sid = db.create_session(cwd=rel_cwd, title="", mode=mode, model=model)
    await manager.get_or_create(sid, cwd=target_cwd, mode=mode, model=model)
    return sid
```

- [ ] **Step 2: Parse + grep for leaked `init_client` references**

```bash
python -c "import ast; ast.parse(open('app/agent/session.py', encoding='utf-8').read()); print('OK')"
grep -rn "init_client" app/ | grep -v __pycache__
```
Expected: empty for `init_client`.

- [ ] **Step 3: Commit**

```bash
git add app/agent/session.py
git commit -m "refactor(agent): session.py becomes a thin manager wrapper"
```

---

## Task 11: Clean `AppState` — remove per-session fields

**Files:**
- Modify: `app/state.py`
- Modify: `app/api/meta.py`
- Modify: `app/api/sessions.py`

- [ ] **Step 1: Edit `app/state.py`** — replace the whole file:

```python
"""Process-level mutable singleton shared by every subsystem.

After Phase 3, the per-session state (client / cwd / mode / model /
turn_lock / current_turn_task / sdk_session_id / client_tz) lives on
ClaudeAgent (`app/agent/agent.py`). This module keeps only truly
process-global state.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket


@dataclass
class AppState:
    cwd_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    websockets: set["WebSocket"] = field(default_factory=set)
    # WS → session_id binding. Set on connect and on cmd:load_session.
    # Lets broadcast_to_agent fan out only to the right subscribers.
    ws_sessions: "dict[WebSocket, str]" = field(default_factory=dict)
    # cb_id → asyncio.Future of the user's allow/deny decision
    pending: "dict[str, asyncio.Future]" = field(default_factory=dict)
    # cb_id → {tool, input, session_id} so reconnecting clients can re-render
    pending_meta: "dict[str, dict]" = field(default_factory=dict)
    # YOLO: process-wide toggle. Not persisted.
    auto_approve: bool = False


state: AppState = AppState()
```

- [ ] **Step 2: Edit `app/api/meta.py:api_health`**

Replace the body with manager-aware version:

```python
import db
from app.agent.manager import manager
# ... (keep existing imports of socket, settings, state, auth_state, _current_device)


@router.get("/api/health")
async def api_health(request: Request):
    base = {"ok": True}
    if auth_state.is_initialized() and _current_device(request) is None:
        return base
    base.update({
        "name": settings.bridge_name or socket.gethostname(),
        "cwd_root": str(state.cwd_root).replace("\\", "/"),
        "active_sessions": manager.active_ids(),
    })
    latest_sid = db.latest_session_id()
    if latest_sid:
        agent = manager.get(latest_sid)
        if agent:
            base.update({
                "session_id": agent.session_id,
                "mode": agent.mode,
                "model": agent.model or "",
            })
    return base
```

- [ ] **Step 3: Edit `app/api/sessions.py`** — replace `api_sessions_list` and `api_sessions_delete`:

```python
import db
from app.agent.manager import manager
# (existing imports preserved)


@router.get("/api/sessions")
async def api_sessions_list(q: str = ""):
    return {
        "current": db.latest_session_id(),
        "sessions": db.search_sessions(q) if q.strip() else db.list_sessions(),
        "query": q,
    }


@router.delete("/api/sessions/{sid}")
async def api_sessions_delete(sid: str):
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "session not found")
    await manager.destroy(sid)
    db.delete_session(sid)
    sdir = uploads_dir() / sid
    if sdir.is_dir():
        with contextlib.suppress(OSError):
            shutil.rmtree(sdir)
    return {"ok": True, "current": db.latest_session_id()}
```

The POST/GET/PATCH session handlers don't read `state.*` (verify via grep) so leave them alone.

- [ ] **Step 4: Parse + grep verify**

```bash
python -c "import ast; [ast.parse(open(p, encoding='utf-8').read()) for p in ['app/state.py', 'app/api/meta.py', 'app/api/sessions.py']]; print('parse OK')"
grep -rn 'state\.\(client\b\|client_tz\|turn_lock\|current_turn_task\|session_id\|sdk_session_id\|\bmode\b\|\bmodel\b\)' app/ | grep -v __pycache__ | grep -v cwd_root
grep -rn 'state\.cwd\b' app/ | grep -v __pycache__ | grep -v cwd_root
```
Expected: no remaining references to removed fields.

- [ ] **Step 5: Commit**

```bash
git add app/state.py app/api/meta.py app/api/sessions.py
git commit -m "refactor(state): drop per-session fields; reroute API readers to manager+db"
```

---

## Task 12: lifespan rewires + warm one session

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Edit lifespan in `app/main.py`**

Replace the existing lifespan body's per-session setup with manager-aware:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    state.cwd_root = Path(settings.default_cwd or os.getcwd()).resolve()
    push.init()
    db.init(state.cwd_root / ".bridge_data" / "bridge.db")
    uploads_dir()
    pb_ready = _pb_refresh_token()
    pb_task = asyncio.create_task(_pb_refresh_loop()) if pb_ready else None
    if not pb_ready and POCKETBASE_URL:
        log.warning("PocketBase configured but initial auth failed — 打卡 will not work")
    report_task = asyncio.create_task(
        report.scheduler_loop(str(state.cwd_root), on_post=_weekly_report_posted)
    )
    # Warm one agent (latest session, or a fresh one if db is empty)
    try:
        from app.agent.session import new_session, open_session
        latest = db.latest_session_id()
        if latest:
            await open_session(latest)
        else:
            await new_session()
    except Exception as e:
        log.exception("initial Claude session warm-up failed: %s", e)
    yield
    if pb_task is not None:
        pb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pb_task
    report_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await report_task
    from app.agent.manager import manager
    await manager.shutdown()
```

Drop the old `state.cwd = state.cwd_root` line (state.cwd no longer exists). The old global-client disconnect block (`if state.client is not None: await state.client.disconnect()`) is replaced by `manager.shutdown()`.

- [ ] **Step 2: Parse**

```bash
python -c "import ast; ast.parse(open('app/main.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "refactor(main): lifespan warms one agent + shuts down manager on exit"
```

---

## Task 13: Deploy + smoke + replay diff (Phase 3 mid-check)

- [ ] **Step 1: Deploy**

```powershell
deploy
```

If health check fails after deploy, check `ssh dashboard-server 'sudo journalctl -u phone-bridge -n 50 --no-pager'`. Most likely cause: a left-over `state.X` reference where X was removed in Task 11.

- [ ] **Step 2: Smoke**

```powershell
$env:BASE = "https://dashboard-server.tail4cfa2.ts.net"
$env:BRIDGE_COOKIE = "bridge_session=<your-cookie>"
python tests/smoke_backend.py
```

Expected: 5/5 green.

- [ ] **Step 3: Replay diff vs phase3_baseline**

```bash
ssh dashboard-server 'sudo mkdir -p /etc/systemd/system/phone-bridge.service.d && sudo tee /etc/systemd/system/phone-bridge.service.d/bridge-record.conf > /dev/null <<EOF
[Service]
Environment="BRIDGE_RECORD=1"
Environment="BRIDGE_RECORD_PATH=/home/dev/phone-bridge/tests/fixtures/phase3_after.jsonl"
EOF
sudo systemctl daemon-reload && rm -f /home/dev/phone-bridge/tests/fixtures/phase3_after.jsonl && sudo systemctl restart phone-bridge && sleep 2'
```

```powershell
python tests/phase2_drive.py
```

```bash
scp dashboard-server:/home/dev/phone-bridge/tests/fixtures/phase3_after.jsonl /tmp/after.jsonl
python tests/replay.py diff tests/fixtures/phase3_baseline.jsonl /tmp/after.jsonl
```

Expected: `OK: <N> records match`. Likely diffs to fix:
- `/api/health` now returns `active_sessions: [...]` → either add `active_sessions` to the replay normalizer to strip it, or accept the schema change in the baseline
- `/api/sessions` `current` value comes from `db.latest_session_id()` now → should be the same value as before (the active session)

If diff fails on schema shape (not random values), document the intended change and re-record baseline:
```bash
cp /tmp/after.jsonl tests/fixtures/phase3_baseline.jsonl
```
This is acceptable since Phase 3 explicitly changes the `/api/health` payload to include the new `active_sessions` field.

- [ ] **Step 4: Real LLM dialog test (single device)**

Manually open PWA, send "用 Read 看一下 README.md 并总结". Observe:
- `工具调用 N` badge appears
- assistant_text streams
- turn_done arrives

This proves Tasks 5-9 wiring is alive.

- [ ] **Step 5: Disable recorder**

```bash
ssh dashboard-server 'sudo rm /etc/systemd/system/phone-bridge.service.d/bridge-record.conf && sudo systemctl daemon-reload && sudo systemctl restart phone-bridge'
```

- [ ] **Step 6: Commit any baseline updates**

```bash
git status  # only if baseline was re-recorded
git add tests/fixtures/phase3_baseline.jsonl  # if changed
git commit -m "test(replay): refresh phase3 baseline post-handler-rewire"  # only if needed
```

---

## Task 14: Two-device concurrency test

**Files:**
- (manual procedure)

The spec demands "两台设备同时连接，互发消息 30 分钟无串扰".

- [ ] **Step 1: Open 2 PWA windows** (phone + laptop, or two browser tabs)

Each connects to https://dashboard-server.tail4cfa2.ts.net/. Each tab gets its own WS connection and its own `state.ws_sessions[ws]` binding.

- [ ] **Step 2: Bind each tab to a different session**

In tab A: open the session drawer, pick session α (or `cmd:new_session`).
In tab B: pick session β.

- [ ] **Step 3: Drive 5 rounds each in parallel**

In tab A: 5× "用 Read 看 /home/dev/phone-bridge/README.md，一句话总结"
In tab B: 5× "ls /home/dev/phone-bridge | head -5" (Bash → permission card)

Verify (REQUIRED):
- Each tab sees ONLY its own assistant_text / tool_use / turn_done streams (no cross-talk)
- Permission card from tab B does NOT appear in tab A
- Cancel in tab A does not cancel tab B's turn
- Switching model in tab A does not interrupt tab B's in-flight turn

- [ ] **Step 4: Check journal**

```bash
ssh dashboard-server 'sudo journalctl -u phone-bridge --since "30 minutes ago" --no-pager | grep -iE "error|exception|traceback" | head -20'
```

Expected: empty.

If interference observed: a `broadcast_to_agent` call is missing somewhere (handler sent via global `broadcast` instead). Grep for stragglers:
```bash
grep -n "await broadcast(" app/agent/turn.py app/agent/permission.py app/ws/handler.py
```
Most `broadcast(` calls in those files should be `broadcast_to_agent(`. The ones that stay global: `auto_approve_changed`, `auto_approve` system msg (both are process-wide events), `session_renamed`, `session_deleted` (db-level changes affecting all session lists).

---

## Task 15: Notion 429/5xx retry + token bucket

**Files:**
- Modify: `notion_sync/notion_api.py`
- Create: `tests/test_notion_api_backoff.py`

Phase 3 Part B — independent of session manager.

- [ ] **Step 1: Write `tests/test_notion_api_backoff.py` (TDD)**

```python
"""Tests for notion_sync.notion_api.NotionClient retry + token bucket."""
import time
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from notion_sync.notion_api import NotionClient


def _mock_response(status: int, body: bytes = b'{}',
                   headers: dict | None = None) -> MagicMock:
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    m.status = status
    m.read = MagicMock(return_value=body)
    m.headers = headers or {}
    return m


def _mock_http_error(code: int, body: bytes = b'{}',
                     headers: dict | None = None):
    from urllib.error import HTTPError
    return HTTPError(url="x", code=code, msg="err",
                     hdrs=headers or {}, fp=BytesIO(body))


@pytest.fixture
def client():
    return NotionClient(token="test-token")


def test_429_with_retry_after_waits_then_succeeds(client, monkeypatch):
    """429 with Retry-After: 0.05 → sleep 50ms → retry → success."""
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise _mock_http_error(429, b'{"error":"rate_limited"}',
                                   {"Retry-After": "0.05"})
        return _mock_response(200, b'{"ok": true}')
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = client._http("GET", "/test")
    assert result == {"ok": True}
    assert len(calls) == 2
    assert (calls[1] - calls[0]) >= 0.05


def test_5xx_retries_with_exponential_backoff(client, monkeypatch):
    """500 → 503 → 200; succeeds on third attempt."""
    seq = [
        _mock_http_error(500, b'{}'),
        _mock_http_error(503, b'{}'),
        _mock_response(200, b'{"ok":true}'),
    ]
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(time.monotonic())
        r = seq[len(calls) - 1]
        if isinstance(r, BaseException): raise r
        return r
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = client._http("GET", "/test")
    assert result == {"ok": True}
    assert len(calls) == 3


def test_4xx_other_than_429_does_not_retry(client, monkeypatch):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _mock_http_error(400, b'{"error":"bad"}')
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        client._http("GET", "/test")
    assert len(calls) == 1


def test_5xx_max_retries_exhausted(client, monkeypatch):
    """5 consecutive 500s → raises after final attempt (1 initial + 4 retries)."""
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _mock_http_error(500, b'{}')
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        client._http("GET", "/test")
    assert len(calls) == 5


def test_token_bucket_allows_burst_then_throttles(monkeypatch):
    """Token bucket: capacity=3, refill=3/sec — first 3 calls fast,
    4th waits ~333ms."""
    c = NotionClient(token="t")
    starts = []
    def fake_urlopen(req, timeout=None):
        starts.append(time.monotonic())
        return _mock_response(200, b'{}')
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    for _ in range(5):
        c._http("GET", "/x")
    # First 3 fast
    assert (starts[2] - starts[0]) < 0.1
    # 4th waits ~333ms
    assert (starts[3] - starts[2]) > 0.2
```

- [ ] **Step 2: Run test → confirm failure**

```bash
.venv/Scripts/pytest tests/test_notion_api_backoff.py -v
# or on VM if local venv lacks pytest:
# ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/pytest tests/test_notion_api_backoff.py -v'
```

Expected: 5 failures (current code has no backoff + uses fixed sleep, not token bucket).

- [ ] **Step 3: Implement in `notion_sync/notion_api.py`**

Replace the file content:

```python
"""Sync wrapper around Notion REST API.

Stdlib urllib only.

Rate-limiting: token bucket (3 capacity, refill 3/sec) — allows short
bursts up to capacity then steady 3 req/s. Phase 3 upgrade from the
previous fixed 0.5s sleep.

Retry: HTTP 429 honors Retry-After header (capped at 30s); 5xx
exponential backoff 0.1/0.2/0.4/0.8s × 4 retries; other 4xx fail fast.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

NOTION_API_VERSION = "2022-06-28"

_BUCKET_CAPACITY = 3
_BUCKET_REFILL_PER_SEC = 3.0
_MAX_RETRIES = 4
_BACKOFF_BASE_SEC = 0.1
_RETRY_AFTER_CAP_SEC = 30.0


class _TokenBucket:
    """Thread-safe token bucket."""
    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity,
                                  self.tokens + elapsed * self.refill_per_sec)
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                deficit = 1.0 - self.tokens
                wait = deficit / self.refill_per_sec
            time.sleep(wait)


class NotionClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ["NOTION_TOKEN"]
        self._bucket = _TokenBucket(_BUCKET_CAPACITY, _BUCKET_REFILL_PER_SEC)

    def _retry_after_sec(self, headers) -> float:
        raw = ""
        try:
            raw = headers.get("Retry-After") if hasattr(headers, "get") else ""
        except Exception:
            raw = ""
        if not raw:
            return 0.0
        try:
            return min(_RETRY_AFTER_CAP_SEC, max(0.0, float(raw)))
        except (TypeError, ValueError):
            return 0.0

    def _http(self, method: str, path: str, body: Any | None = None) -> Any:
        url = f"https://api.notion.com/v1{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)

        for attempt in range(_MAX_RETRIES + 1):
            self._bucket.take()
            try:
                with urllib.request.urlopen(req, timeout=30.0) as r:
                    raw = r.read().decode("utf-8")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < _MAX_RETRIES:
                    wait = self._retry_after_sec(e.headers) or \
                           (_BACKOFF_BASE_SEC * (2 ** attempt))
                    time.sleep(wait)
                    continue
                if 500 <= e.code < 600 and attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_BASE_SEC * (2 ** attempt))
                    continue
                raw = e.read().decode("utf-8", "replace")
                raise RuntimeError(
                    f"Notion {method} {path}: {e.code} {raw[:500]}") from None
        raise RuntimeError(f"Notion {method} {path}: retries exhausted")

    def query_database(self, database_id: str, *,
                       filter_: dict | None = None,
                       sorts: list[dict] | None = None,
                       page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        start_cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if filter_: body["filter"] = filter_
            if sorts: body["sorts"] = sorts
            if start_cursor: body["start_cursor"] = start_cursor
            data = self._http("POST", f"/databases/{database_id}/query", body=body)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")
        return out

    def retrieve_database(self, database_id: str) -> dict:
        return self._http("GET", f"/databases/{database_id}")

    def update_database(self, database_id: str, body: dict) -> dict:
        return self._http("PATCH", f"/databases/{database_id}", body=body)

    def create_database(self, parent_page_id: str, title: str,
                        properties: dict) -> dict:
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        return self._http("POST", "/databases", body=body)

    def retrieve_page(self, page_id: str) -> dict:
        return self._http("GET", f"/pages/{page_id}")

    def create_page(self, database_id: str, properties: dict,
                    icon: dict | None = None) -> dict:
        body: dict = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        if icon is not None:
            body["icon"] = icon
        return self._http("POST", "/pages", body=body)

    def update_page(self, page_id: str, properties: dict | None = None,
                    archived: bool | None = None,
                    icon: dict | None = None) -> dict:
        body: dict[str, Any] = {}
        if properties is not None: body["properties"] = properties
        if archived is not None: body["archived"] = archived
        if icon is not None: body["icon"] = icon
        return self._http("PATCH", f"/pages/{page_id}", body=body)
```

- [ ] **Step 4: Run tests → pass**

```bash
.venv/Scripts/pytest tests/test_notion_api_backoff.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add notion_sync/notion_api.py tests/test_notion_api_backoff.py
git commit -m "feat(notion): 429+Retry-After + 5xx exp backoff + token bucket throttle"
```

---

## Task 16: Final deploy + replay + 48h soak

- [ ] **Step 1: Deploy**

```powershell
deploy
```

- [ ] **Step 2: Run all unit tests on VM**

```bash
scp tests/fakes/__init__.py tests/fakes/sdk_client.py tests/test_session_manager.py tests/test_notion_api_backoff.py dashboard-server:/home/dev/phone-bridge/tests/
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/pytest tests/test_session_manager.py tests/test_notion_api_backoff.py tests/test_pb_client.py tests/test_settings.py tests/test_io_utils.py -v 2>&1 | tail -25'
```

Expected: all green (test_session_manager 6/6, test_notion_api_backoff 5/5, plus pre-existing Phase 0/1 tests).

- [ ] **Step 3: Smoke + replay diff**

```powershell
python tests/smoke_backend.py
```

```bash
ssh dashboard-server 'sudo mkdir -p /etc/systemd/system/phone-bridge.service.d && sudo tee /etc/systemd/system/phone-bridge.service.d/bridge-record.conf > /dev/null <<EOF
[Service]
Environment="BRIDGE_RECORD=1"
Environment="BRIDGE_RECORD_PATH=/home/dev/phone-bridge/tests/fixtures/phase3_final.jsonl"
EOF
sudo systemctl daemon-reload && rm -f /home/dev/phone-bridge/tests/fixtures/phase3_final.jsonl && sudo systemctl restart phone-bridge && sleep 2'
```

```powershell
python tests/phase2_drive.py
```

```bash
scp dashboard-server:/home/dev/phone-bridge/tests/fixtures/phase3_final.jsonl /tmp/final.jsonl
python tests/replay.py diff tests/fixtures/phase3_baseline.jsonl /tmp/final.jsonl
```

Expected: `OK: <N> records match`.

- [ ] **Step 4: Disable recorder for soak**

```bash
ssh dashboard-server 'sudo rm /etc/systemd/system/phone-bridge.service.d/bridge-record.conf && sudo systemctl daemon-reload && sudo systemctl restart phone-bridge'
```

- [ ] **Step 5: 48h soak**

Let staging run for 48 hours. After 48h:

```bash
ssh dashboard-server 'sudo journalctl -u phone-bridge --since "48 hours ago" --no-pager | grep -iE "error|exception|traceback" | wc -l'
```
Expected: 0.

Use the PWA daily during this window — real conversations, switching models, switching cwd, multiple devices when convenient.

---

## Task 17: Strip recorder + Phase 3 completion report

- [ ] **Step 1: Remove BRIDGE_RECORD scaffolding from `app/main.py` and `app/ws/handler.py`**

In `app/main.py`, delete the `# --- Phase 3 baseline recorder ---` block + the `_record_http` middleware (lines added in Task 0 Step 2). Remove `Request` from the `from fastapi import ...` line if no longer used.

In `app/ws/handler.py`, delete the `_recorder()` helper and the `rec = _recorder()` block (Task 0 Step 3). Remove the `if rec: rec.ws_close(None)` from the finally block.

- [ ] **Step 2: Smoke + final deploy**

```bash
python tests/smoke_backend.py
deploy
python tests/smoke_backend.py
```

- [ ] **Step 3: Write Phase 3 completion report**

Append to `CHANGELOG.md` immediately above the Phase 2 entry. Follow §完成报告模板 structure from previous phases (Branch / 工时 / 落地的事 / 闸门 / 偏离计划 / 量化 / 下一步).

- [ ] **Step 4: Update spec progress table**

`docs/superpowers/specs/2026-06-06-refactor-roadmap.md`:
- Phase 3 row: `🚧 进行中` → `✅ 已合并` (after Step 5 merge), date 2026-06-XX, merge SHA, `CHANGELOG §Phase 3`
- 下一步入口 → Phase 4 · 前端模块化

- [ ] **Step 5: Commit + invoke finishing-a-development-branch**

```bash
git add app/main.py app/ws/handler.py CHANGELOG.md docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(changelog): Phase 3 completion report + strip recorder"
```

Then invoke `superpowers:finishing-a-development-branch` and choose Option 1 (Merge locally) since soak passed.

---

## Self-Review

**1. Spec coverage:**

| Spec 动作 | Plan task |
|---|---|
| `app/agent/manager.py`: `SessionManager` `Dict[session_id, ClaudeAgent]` | Task 3 |
| WS handler 改造：路由消息到对应 ClaudeAgent | Task 9 |
| `set_model` / cwd 切换走 `SessionManager.recreate(session_id)` | Task 9 (`handle_cmd` set_model / cwd) |
| `init_client` / recreate 之前先 `await turn_lock` | Task 3 (`recreate` 持 turn_lock) + Task 4 测试覆盖 |
| 老的全局 `state.client` 移除；`AppState` 字段相应清理 | Task 11 |
| `notion_sync/notion_api.py` 加 429/5xx 退避 + `Retry-After` 头识别 | Task 15 |
| Notion `_throttle` 改 token bucket | Task 15 |
| `tests/test_session_manager.py`：两并发 session、permission_request 互不干扰、recreate 时 in-flight turn 安全终止 | Task 4 (6 tests) |
| 准出：两台设备同时连接 30 分钟无串扰 | Task 14 |
| 准出：Notion linkage PATCH 墙时 < 5 秒 | Task 15 (token bucket allows burst then 333ms steady) |
| 准出：staging 48h | Task 16 Step 5 |

✅ 全部覆盖。

**2. Placeholder scan:** 无 TBD / TODO。每个 step 都有可执行代码或精确命令。

**3. Type consistency:**
- `ClaudeAgent` 字段名整篇一致（session_id, cwd, mode, model, sdk_session_id, client, turn_lock, current_turn_task, client_tz）
- `SessionManager.get / get_or_create / recreate / destroy / shutdown / active_ids` 签名在 Task 3 定，后续 Task 9-12 调用一致
- `broadcast_to_agent(agent, msg)` 签名 Task 8 定，Task 6/7/9 一致
- `run_user_turn(agent, text, images, files)` Task 6 定，Task 9 一致
- `make_options(agent)` Task 5 定，Task 3 调用一致
- `_save_msg(agent, role, content)` Task 6 改签名

**4. Order dependencies:**
- Task 0 (baseline) → 1 (Agent) → 2 (FakeClient) → 3 (Manager) → 4 (Tests) → 5 (make_options) → 6 (turn + broadcast stub) → 7 (permission) → 8 (real broadcast_to_agent) → 9 (handler) → 10 (session shim) → 11 (state cleanup) → 12 (lifespan) → 13 (mid-deploy)。Task 14（双设备）和 Task 15（Notion）独立，可并行。Task 16-17 收尾。
- Task 6 用 stub 让 Task 8 之前可以 commit；Task 8 替换 stub。

**5. Honest scope:**
- 17 tasks，~20 commits，3-5 天 wall-clock（含 48h soak）
- 每个 task 独立 revertible
- 测试三重保险：unit tests (6+5) + replay diff (driver 不变) + manual 2-device test
- Notion 改动通过 mock 单测验证；生产正确性靠 sync 日志观察

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-08-phase-3-session-manager.md`.**

两种执行方式：

**1. Subagent-Driven (recommended)** — 每个 task 派一个新 subagent 跑，task 之间复查，迭代快

**2. Inline Execution** — 当前会话里跑，按 task batch + checkpoints

哪一种？
