# Phase 6a · 测试补齐 + structlog 统一日志 + 文档清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 5 个关键模块的单元测试（auth_middleware / db_crud / build_user_content / can_use_tool / run_user_turn），引入 `structlog` + contextvars 让每条日志自动带 `session_id` / `cb_id`，清理过期文档。

**Architecture:**
- **Phase 6a 范围严格收缩到 SAFE 项**：所有改动只增不改既有 auth 行为。今日 `feature/hidden-auth-superlink`（commits `af00f8f..20e3215`）的 super-link 模型必须保持原样。CSRF / SameSite Strict / Origin 校验留给 Phase 6b 单独讨论。
- **测试策略**：5 个新 test 文件全部用 mock + pytest fixtures，不动 prod 代码（FakeClient 复用 Phase 3 `tests/fakes/`）。`test_auth_middleware.py` 用 FastAPI `TestClient` 测 4 条路径（public_exact / super-link gate / device cookie / decoy 503）。
- **structlog 迁移**：新建 `app/log.py:get_logger(name)` 返回 BoundLogger，自动从 contextvars 注入 `session_id` / `cb_id`。15 个用 `logging.getLogger("bridge")` 的文件改成 `from app.log import get_logger; log = get_logger("bridge")`，输出从 stdlib text 改为 JSON-per-line。`notion_sync/logger.py` 不动（独立 sync event log，已 RotatingFileHandler）。`auth.py` 不动（今日 super-link 代码）。

**Tech Stack:** `structlog` (新 dep)、`pytest`、`fastapi.testclient.TestClient`、Python 3.11 contextvars。

**Branch:** `refactor/phase-6a-tests-structlog` (已创建，从 `20e3215`)
**Parent spec:** [2026-06-06-refactor-roadmap.md](../specs/2026-06-06-refactor-roadmap.md) §Phase 6
**Roadmap 风险标识：** 低（所有改动只增不改 auth 行为；测试 read-only；structlog 只换日志输出格式）

---

## 🔒 必须保持原样（来自 2026-06-10 super-link 工作）

| 文件 / 符号 | 状态 |
|---|---|
| `app/auth/middleware.py:auth_middleware` 4 条路径逻辑 | 不动 |
| `app/auth/middleware.py:_current_device` 公开 helper | 不动 |
| `app/auth/middleware.py:_PUBLIC_EXACT` 三项白名单 | 不动 |
| `app/auth/middleware.py:_DECOY_BODY` + 503 + `Retry-After: 120` | 不动 |
| `app/auth/gate.py:superlink_gate` | 不动 |
| `app/auth/state.py:auth_state` + `verify_super_link` / `lookup_token` | 不动 |
| `auth.py:COOKIE_NAME` / `client_ip` / `set_session_cookie` | 不动 |
| `auth.py:302` cookie `samesite="lax"` | **不动**（Phase 6b 改 Strict）|
| 90-day sliding cookie refresh | 不动 |
| super-link 路径分发：`path 第一段 == 密钥 → superlink_gate` | 不动 |

测试 `test_auth_middleware.py` 只 read-only 验证上述行为。

---

## File Structure (Target)

```
app/
  log.py                          # 新：get_logger + contextvars 注入
  agent/{content,manager,options,permission,session,turn}.py  # 改：log import
  api/{poi,sync,today_todos}.py   # 改：log import
  integrations/pb/{client,token}.py # 改：log import
  main.py                         # 改：log import + configure() at lifespan start
  reporting/weekly_report.py      # 改：log import
  ws/handler.py                   # 改：log import

notion_sync/logger.py             # 不动
auth.py + app/auth/*              # 不动

tests/
  test_auth_middleware.py         # 新：4 paths × TestClient
  test_db_crud.py                 # 新：CRUD on tmp sqlite
  test_build_user_content.py      # 新：xlsx / text / image 上传解析
  test_can_use_tool.py            # 新：permission 4 paths
  test_run_user_turn.py           # 新：turn pipeline via FakeClient
  fakes/sdk_client.py             # 可能加 queue_assistant_text / queue_result helper (额外 only)

requirements.in                   # 改：加 structlog
requirements.txt                  # 改：pip-compile 重生

CHANGELOG.md                      # 改：Phase 6a 完成报告
docs/superpowers/specs/2026-06-06-refactor-roadmap.md  # 改：Phase 6a 状态
```

**Out of scope (Phase 6b/6c):**
- CSRF 双提交 token 中间件
- cookie SameSite=Strict
- Origin 校验中间件
- request_id auto-injection (需 middleware 位置，和 auth-adjacent 中间件一起做)
- OTel hook 接入 Sentry

---

## Pre-Flight Notes

### structlog `configure()`（idempotent）

```python
import contextvars, logging, structlog

_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
_cb_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("cb_id", default=None)


def _inject_contextvars(_, __, event_dict):
    sid = _session_id.get()
    if sid: event_dict["session_id"] = sid
    cb = _cb_id.get()
    if cb: event_dict["cb_id"] = cb
    return event_dict


def configure(level=logging.INFO):
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_contextvars,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, format="%(message)s", force=True)


def get_logger(name="bridge"): return structlog.get_logger(name)
def bind_session(sid): _session_id.set(sid)
def bind_cb(cb): _cb_id.set(cb)
```

callsites:
- `app/agent/turn.py:run_user_turn` 顶部 `bind_session(agent.session_id)`
- `app/agent/permission.py:can_use_tool` 在 cb_id 生成后 `bind_cb(cb_id)`
- `request_id` 自动注入推到 Phase 6b（要新中间件位置，避免动 auth_middleware）

### 验证策略
- **Per task**：相关 pytest + smoke
- **Final**：deploy + ~166 tests + smoke 5/5 + journal JSON 化

---

## Task 0: 预备 - 加 structlog dep + 新 `app/log.py`（不接入）

**Files:**
- Modify: `requirements.in`
- Modify: `requirements.txt`
- Create: `app/log.py`

- [ ] **Step 1: 加 dep**

```bash
cd "/d/Projects/Phone Bridge"
# 直接 echo 追加
echo "structlog~=25.0" >> requirements.in
```

- [ ] **Step 2: 重 compile**

VM 端 compile（保证锁定版本与 prod 一致）：

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/python -m piptools compile --output-file requirements.txt --strip-extras requirements.in 2>&1 | tail -5'
scp dashboard-server:/home/dev/phone-bridge/requirements.txt requirements.txt
```

Expected: requirements.txt 多一行 `structlog==25.x.x` + 其依赖（无）。

- [ ] **Step 3: 写 `app/log.py`**

```python
"""Structured logging foundation (Phase 6a Task 0).

Replaces stdlib `logging.getLogger("bridge")` with structlog that
automatically injects `session_id` / `cb_id` from contextvars into
every log line. JSON-per-line output for systemd journal.

`configure()` is called once from `app/main.py` lifespan.
Modules use `get_logger("bridge")`. Callers bind contextvars:
- `bind_session(sid)` — turn boundary in agent/turn.py
- `bind_cb(id)` — permission gate boundary in agent/permission.py

request_id auto-injection is deferred to Phase 6b alongside the
CSRF/Origin work that adds the request-id middleware.
"""
from __future__ import annotations

import contextvars
import logging

import structlog


_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None)
_cb_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cb_id", default=None)


def _inject_contextvars(_, __, event_dict):
    sid = _session_id.get()
    if sid:
        event_dict["session_id"] = sid
    cb = _cb_id.get()
    if cb:
        event_dict["cb_id"] = cb
    return event_dict


def configure(level: int = logging.INFO) -> None:
    """Idempotent setup. Call once from lifespan."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_contextvars,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # Route stdlib logging through the JSON renderer too (so
    # uvicorn/fastapi access logs also become JSON). force=True
    # overrides any prior basicConfig.
    logging.basicConfig(level=level, format="%(message)s", force=True)


def get_logger(name: str = "bridge"):
    """structlog BoundLogger keyed by `name`."""
    return structlog.get_logger(name)


def bind_session(session_id: str | None) -> None:
    """Set session_id for the current async context. Auto-injected
    into every subsequent log line in this task/turn."""
    _session_id.set(session_id)


def bind_cb(cb_id: str | None) -> None:
    """Set cb_id (permission callback id) for the current scope."""
    _cb_id.set(cb_id)
```

- [ ] **Step 4: Sanity import**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/pip install "structlog~=25.0" 2>&1 | tail -2'
scp app/log.py dashboard-server:/home/dev/phone-bridge/app/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/python -c "
from app.log import get_logger, configure, bind_session, bind_cb
configure()
log = get_logger(\"bridge\")
bind_session(\"sess-xyz\")
bind_cb(\"cb-abc\")
log.info(\"smoke_test\", phase=\"6a\")
print(\"OK\")
"'
```

Expected: JSON line printed containing `"session_id": "sess-xyz"`, `"cb_id": "cb-abc"`, `"event": "smoke_test"`, then `OK`.

- [ ] **Step 5: Commit**

```bash
git add requirements.in requirements.txt app/log.py
git commit -m "feat(log): add structlog dep + app/log.py foundation (not wired yet)

Phase 6a Task 0. structlog~=25.0 added to requirements. New module
app/log.py provides:
- configure() — idempotent JSON renderer + stdlib bridge
- get_logger(name) — BoundLogger factory
- bind_session(sid) / bind_cb(id) — contextvar setters auto-injected

Not wired into modules yet (Task 6 swaps callsites once tests prove
no behavior regression). configure() not called from lifespan yet."
```

## Critical rules for Task 0

- DO NOT touch any module that uses `logging.getLogger("bridge")` (Task 6's job)
- DO NOT call `configure()` from `app/main.py` yet (Task 6)
- DO confirm commit only touches: requirements.in, requirements.txt, app/log.py

---

## Task 1: `tests/test_auth_middleware.py` — 4 paths via TestClient

**Files:**
- Create: `tests/test_auth_middleware.py`

唯一直接 touch auth 的 task，但 read-only。

- [ ] **Step 1: 看 auth_state 接口**

```bash
cat app/auth/state.py
```

记下 `verify_super_link` / `lookup_token` 签名（mock 时签名要保持一致）。

- [ ] **Step 2: 写 test 文件**

```python
"""Auth middleware path coverage.

Phase 6a Task 1. Read-only tests of the 4 paths exposed by today's
hidden-auth-superlink middleware:
  1. _PUBLIC_EXACT (/api/health, OAuth well-known) → pass through
  2. super-link first segment match → superlink_gate
  3. valid device cookie → real app
  4. everything else → 503 decoy
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

import auth as auth_mod
from app.auth.middleware import auth_middleware, _DECOY_BODY
from app.auth.state import auth_state


@pytest.fixture
def app():
    a = FastAPI()
    a.middleware("http")(auth_middleware)

    @a.get("/api/health")
    async def health():
        return {"ok": True}

    @a.get("/anything-else")
    async def anything():
        return {"ok": "secret"}

    @a.get("/.well-known/oauth-protected-resource/mcp")
    async def wkn():
        return {"ok": "wk"}

    return a


def test_public_exact_passes_through(app):
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_oauth_well_known_passes_through(app):
    with TestClient(app) as c:
        r = c.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200


def test_no_cookie_returns_503_decoy(app):
    with TestClient(app) as c:
        r = c.get("/anything-else")
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "120"
    assert r.content == _DECOY_BODY
    assert "nginx" in r.text


def test_invalid_cookie_returns_503_decoy(app):
    with patch.object(auth_state, "lookup_token", return_value=None):
        with TestClient(app) as c:
            r = c.get("/anything-else",
                      cookies={auth_mod.COOKIE_NAME: "garbage"})
    assert r.status_code == 503
    assert "nginx" in r.text


def test_valid_cookie_passes_through(app):
    device = {"id": "dev1", "name": "phone"}
    with patch.object(auth_state, "lookup_token", return_value=device):
        with TestClient(app) as c:
            r = c.get("/anything-else",
                      cookies={auth_mod.COOKIE_NAME: "validtoken"})
    assert r.status_code == 200
    assert r.json() == {"ok": "secret"}


def test_super_link_first_segment_dispatches_to_gate(app):
    async def fake_gate(req):
        return HTMLResponse("<form>fake-gate</form>", status_code=200)

    with patch.object(auth_state, "verify_super_link",
                      side_effect=lambda seg: seg == "secretpath"):
        with patch("app.auth.middleware.superlink_gate", new=fake_gate):
            with TestClient(app) as c:
                r = c.get("/secretpath")
    assert r.status_code == 200
    assert "fake-gate" in r.text


def test_super_link_wrong_segment_returns_decoy(app):
    with patch.object(auth_state, "verify_super_link", return_value=False):
        with TestClient(app) as c:
            r = c.get("/some-random-string")
    assert r.status_code == 503


def test_root_path_no_cookie_returns_decoy(app):
    with patch.object(auth_state, "verify_super_link", return_value=False):
        with TestClient(app) as c:
            r = c.get("/")
    assert r.status_code == 503
```

- [ ] **Step 3: Run**

```bash
scp tests/test_auth_middleware.py dashboard-server:/home/dev/phone-bridge/tests/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_auth_middleware.py -v 2>&1 | tail -15'
```

Expected: 8/8 pass. If a test fails because `verify_super_link` signature differs, adjust the test side_effect lambda to match; don't touch prod.

- [ ] **Step 4: Commit**

```bash
git add tests/test_auth_middleware.py
git commit -m "test(auth): cover 4 middleware paths (Phase 6a Task 1)

Read-only tests for today's hidden-auth-superlink middleware:
- /api/health + /.well-known/oauth-* → 200 (public_exact)
- valid cookie → 200 real app
- super-link first segment match → fake gate (patched)
- everything else → 503 decoy

8 tests, FastAPI TestClient + patch on auth_state. No prod code touched."
```

## Critical rules for Task 1

- DO NOT modify `app/auth/*.py` or `auth.py` in any way
- DO use `patch.object(auth_state, ...)` not extend it
- DO confirm `git log -1 --stat` shows ONLY the new test file

---

## Task 2: `tests/test_db_crud.py` — db.py CRUD coverage

**Files:**
- Create: `tests/test_db_crud.py`

- [ ] **Step 1: 看 db.py 接口**

```bash
grep -nE "^def " db.py
```

主要函数：`init / create_session / list_sessions / search_sessions / get_session / append_message / update_session / append_turn / usage_summary / range_summary / get_setting / set_setting / delete_session / latest_session_id`

- [ ] **Step 2: 写 test 文件**

```python
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
    msg_id = db.append_message(sid, "user", {"text": "hi"})
    assert isinstance(msg_id, int)
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
    sessions = summary.get("sessions") or []
    matching = [s for s in sessions if s.get("id") == sid]
    assert matching, f"session {sid} not in usage summary: {summary}"
    s = matching[0]
    assert s.get("input_tokens") == 100
    assert s.get("output_tokens") == 200
    assert s.get("cost_usd") == pytest.approx(0.05)


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
```

- [ ] **Step 3: Run + commit**

```bash
scp tests/test_db_crud.py dashboard-server:/home/dev/phone-bridge/tests/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_db_crud.py -v 2>&1 | tail -15'
```

Expected: 11/11 pass. If any return-shape assumption is wrong, adjust the test to match prod's actual shape; don't touch db.py.

```bash
git add tests/test_db_crud.py
git commit -m "test(db): cover session/message/turn CRUD (Phase 6a Task 2)

11 tests on tmp_path sqlite — create/get/update/delete + list/search +
append_message + append_turn → usage_summary + setting roundtrip.
No prod code touched."
```

## Critical rules for Task 2

- DO NOT modify `db.py`; if a test misses prod's actual shape, fix the test
- DO use `tmp_path` so no production bridge.db contact
- DO confirm `git log -1 --stat` shows ONLY the new test file

---

## Task 3: `tests/test_build_user_content.py` — content.py 解析

**Files:**
- Create: `tests/test_build_user_content.py`

- [ ] **Step 1: 看 content.py 接口**

```bash
sed -n '1,80p' app/agent/content.py
```

- [ ] **Step 2: 写 test 文件**

```python
"""build_user_content + xlsx/text upload parsing.

Phase 6a Task 3. Tests app/agent/content.py — the function that turns
uploaded image/file paths + chat text into the SDK content array.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from app.agent.content import (
    _build_user_content, _read_text_safe, _read_xlsx_as_text,
)


def test_text_only_message():
    out = _build_user_content("hello world", [], [])
    text_blocks = [b for b in out if b.get("type") == "text"]
    assert any("hello world" in b["text"] for b in text_blocks)


def test_empty_text_with_no_attachments_no_crash():
    out = _build_user_content("", [], [])
    assert isinstance(out, list)
    assert all(isinstance(b, dict) for b in out)


def test_text_file_attachment_inlined(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("LINE-A\nLINE-B\n", encoding="utf-8")
    out = _build_user_content("see file", [], [str(p)])
    serialized = str(out)
    assert "LINE-A" in serialized
    assert "LINE-B" in serialized


def test_unknown_extension_still_attempts_read(tmp_path):
    p = tmp_path / "data.log"
    p.write_text("logline-42", encoding="utf-8")
    out = _build_user_content("", [], [str(p)])
    assert "logline-42" in str(out)


def test_read_text_safe_handles_missing_file():
    bad = _read_text_safe(Path("/no/such/file.txt"))
    assert isinstance(bad, str)


def test_read_text_safe_handles_binary_file(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x00\x01\x02\x03BIN\xff")
    out = _read_text_safe(p)
    assert isinstance(out, str)


def test_read_xlsx_as_text_handles_simple_file(tmp_path):
    try:
        from openpyxl import Workbook
    except ImportError:
        pytest.skip("openpyxl not installed in test env")

    p = tmp_path / "data.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["name", "age"])
    ws.append(["alice", 30])
    ws.append(["bob", 25])
    wb.save(p)

    out = _read_xlsx_as_text(p)
    assert "alice" in out
    assert "bob" in out
    assert "name" in out


def test_image_path_produces_image_block(tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    out = _build_user_content("look at this", [str(img)], [])
    has_image = any(b.get("type") == "image" for b in out)
    assert has_image, f"no image block in output: {out}"
```

- [ ] **Step 3: Run + commit**

```bash
scp tests/test_build_user_content.py dashboard-server:/home/dev/phone-bridge/tests/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_build_user_content.py -v 2>&1 | tail -15'
```

Expected: 8/8 pass.

```bash
git add tests/test_build_user_content.py
git commit -m "test(content): cover build_user_content xlsx/text/image (Phase 6a Task 3)

8 tests covering app/agent/content.py: text-only / empty / .txt /
unknown ext / missing / binary / xlsx / image. No prod code touched."
```

---

## Task 4: `tests/test_can_use_tool.py` — permission gate paths

**Files:**
- Create: `tests/test_can_use_tool.py`

- [ ] **Step 1: 先 ensure pytest-asyncio installed**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/pip show pytest-asyncio 2>&1 | head -2'
```

If not installed (likely it IS installed since Phase 3 had async tests):

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/pip install pytest-asyncio 2>&1 | tail -2'
```

- [ ] **Step 2: 写 test 文件**

```python
"""permission.can_use_tool path coverage.

Phase 6a Task 4. Tests app/agent/permission.py — the SDK permission
callback that gates tool calls. 4 paths:
1. Bash PB-curl fast path (localhost:8090) → allow
2. AUTO_ALLOW tool name → allow
3. state.auto_approve global → allow + system broadcast
4. Normal gate → broadcast permission_request + await future
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from app.agent.permission import can_use_tool, AUTO_ALLOW
from app.state import state


@pytest.fixture(autouse=True)
def reset_state():
    state.pending.clear()
    state.pending_meta.clear()
    state.auto_approve = False
    yield
    state.pending.clear()
    state.pending_meta.clear()
    state.auto_approve = False


@pytest.mark.asyncio
async def test_pb_curl_fast_path_allows(monkeypatch):
    monkeypatch.setattr("app.agent.permission.settings.pocketbase_url",
                        "http://127.0.0.1:8090", raising=False)
    result = await can_use_tool("Bash",
        {"command": "curl http://127.0.0.1:8090/api/health"}, None)
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_pb_curl_localhost_alias_allows(monkeypatch):
    monkeypatch.setattr("app.agent.permission.settings.pocketbase_url",
                        "http://localhost:8090", raising=False)
    result = await can_use_tool("Bash",
        {"command": "curl localhost:8090/api/collections"}, None)
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_auto_allow_tool_passes_without_prompt():
    sample_tool = next(iter(AUTO_ALLOW))
    result = await can_use_tool(sample_tool, {}, None)
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_auto_approve_global_flag_allows_and_broadcasts(monkeypatch):
    state.auto_approve = True
    broadcast_mock = AsyncMock()
    monkeypatch.setattr("app.agent.permission.broadcast", broadcast_mock)

    result = await can_use_tool("UnknownTool", {"x": 1}, None)
    assert isinstance(result, PermissionResultAllow)
    calls_text = [str(c) for c in broadcast_mock.call_args_list]
    assert any("auto-approved" in c for c in calls_text), \
        f"expected auto-approved broadcast, got: {calls_text}"


@pytest.mark.asyncio
async def test_normal_gate_broadcasts_request_and_times_out(monkeypatch):
    broadcast_mock = AsyncMock()
    monkeypatch.setattr("app.agent.permission.broadcast", broadcast_mock)
    monkeypatch.setattr("app.agent.permission.push.send_to_all",
                        lambda *a, **kw: None)
    async def _instant_timeout(fut, timeout):
        raise asyncio.TimeoutError()
    monkeypatch.setattr("app.agent.permission.asyncio.wait_for",
                        _instant_timeout)

    result = await can_use_tool("UnknownTool", {"x": 1}, None)
    assert isinstance(result, PermissionResultDeny)
    calls = [c.args[0] for c in broadcast_mock.call_args_list if c.args]
    assert any(c.get("type") == "permission_request" for c in calls)


@pytest.mark.asyncio
async def test_normal_gate_allow_decision():
    """When the future resolves with 'allow', returns Allow."""
    async def fake_broadcast(msg):
        if isinstance(msg, dict) and msg.get("type") == "permission_request":
            cb_id = msg["id"]
            await asyncio.sleep(0.01)
            fut = state.pending.get(cb_id)
            if fut and not fut.done():
                fut.set_result("allow")

    with patch("app.agent.permission.broadcast", new=fake_broadcast):
        with patch("app.agent.permission.push.send_to_all",
                   lambda *a, **kw: None):
            result = await can_use_tool("UnknownTool", {"x": 1}, None)

    assert isinstance(result, PermissionResultAllow)


def test_auto_allow_set_non_empty():
    assert len(AUTO_ALLOW) > 0
```

- [ ] **Step 3: Run + commit**

```bash
scp tests/test_can_use_tool.py dashboard-server:/home/dev/phone-bridge/tests/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_can_use_tool.py -v 2>&1 | tail -15'
```

Expected: 7/7 pass.

```bash
git add tests/test_can_use_tool.py
git commit -m "test(permission): cover can_use_tool 4 paths (Phase 6a Task 4)

7 tests: PB curl fast path / AUTO_ALLOW / auto_approve broadcast /
gate timeout → deny / gate allow decision / AUTO_ALLOW non-empty
sanity. No prod code touched."
```

---

## Task 5: `tests/test_run_user_turn.py` — turn.py via FakeClient

**Files:**
- Create: `tests/test_run_user_turn.py`
- Maybe modify: `tests/fakes/sdk_client.py` (only if extension needed)

- [ ] **Step 1: 看现有 FakeClient**

```bash
cat tests/fakes/sdk_client.py
```

确认能模拟 `client.query(stream)` + `client.receive_response()` async iterator。

- [ ] **Step 2: 如需要，加 helpers 到 FakeClient**

如果 FakeClient 没有 `queue_assistant_text` / `queue_result` 之类的 queue API，加最小补丁。**只增不改既有 contract**:

```python
# tests/fakes/sdk_client.py — additions, leave the rest alone

# At top of class FakeClient.__init__ (if not already present):
self._queued_blocks = []
self._queued_result = None


def queue_assistant_text(self, text: str) -> None:
    """Test helper: queue an AssistantMessage with a single TextBlock."""
    from claude_agent_sdk import AssistantMessage, TextBlock
    self._queued_blocks.append(
        AssistantMessage(content=[TextBlock(text=text)])
    )


def queue_result(self, *, cost_usd: float = 0.0,
                 input_tokens: int = 0, output_tokens: int = 0,
                 duration_ms: int = 0, num_turns: int = 1) -> None:
    """Test helper: queue the terminating ResultMessage."""
    from claude_agent_sdk import ResultMessage
    self._queued_result = ResultMessage(
        session_id="fake-sdk-sess",
        total_cost_usd=cost_usd,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        duration_ms=duration_ms,
        num_turns=num_turns,
    )
```

And the `receive_response` async iterator must yield queued blocks then the ResultMessage:

```python
async def receive_response(self):
    for blk in self._queued_blocks:
        yield blk
    if self._queued_result is not None:
        yield self._queued_result
```

If `receive_response` already exists with a different yield pattern, **don't break it** — extend rather than replace. Look at what existing `test_session_manager.py` expects from FakeClient and preserve.

- [ ] **Step 3: 写 test 文件**

```python
"""run_user_turn end-to-end via FakeClient.

Phase 6a Task 5. Tests app/agent/turn.py — the per-turn pipeline that
takes a user message, queries the SDK, streams responses, and writes
db rows. Uses Phase 3's tests/fakes/sdk_client.py:FakeClient.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import db
from app.agent.agent import ClaudeAgent
from app.agent.turn import run_user_turn
from tests.fakes.sdk_client import FakeClient


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db.init(tmp_path / "test_bridge.db")


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
```

- [ ] **Step 4: Run + commit**

```bash
scp tests/test_run_user_turn.py tests/fakes/sdk_client.py dashboard-server:/home/dev/phone-bridge/tests/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_run_user_turn.py tests/test_session_manager.py -v 2>&1 | tail -15'
```

Expected: 4 new + existing test_session_manager (6) = 10 total pass. If FakeClient extension broke test_session_manager, revert the FakeClient changes and use a different mocking strategy in test_run_user_turn (e.g., raw `AsyncMock` for `agent.client`).

```bash
git add tests/test_run_user_turn.py tests/fakes/sdk_client.py
git commit -m "test(turn): cover run_user_turn pipeline via FakeClient (Phase 6a Task 5)

4 tests covering app/agent/turn.py:
- assistant_text → db row persisted
- ResultMessage → turn_done broadcast with cost_usd
- agent.client is None → error broadcast, no crash
- empty title + first user message → auto-titled

FakeClient (from Phase 3) extended with queue_assistant_text /
queue_result helpers (additive, doesn't break test_session_manager).
No prod code touched."
```

## Critical rules for Task 5

- DO NOT modify any prod file
- DO run `test_session_manager.py` BEFORE committing to ensure FakeClient extension doesn't regress
- DO confirm `git log -1 --stat` shows at most 2 files: test_run_user_turn.py + fakes/sdk_client.py (if extended)

---

## Task 6: Wire structlog — `configure()` from lifespan + 15 callsite migrations

**Files:**
- Modify: `app/main.py`
- Modify: `app/agent/{content,manager,options,permission,session,turn}.py` (6 files)
- Modify: `app/api/{poi,sync,today_todos}.py` (3 files)
- Modify: `app/integrations/pb/{client,token}.py` (2 files)
- Modify: `app/reporting/weekly_report.py`
- Modify: `app/ws/handler.py`
- Modify: `app/agent/turn.py` (also `bind_session`)
- Modify: `app/agent/permission.py` (also `bind_cb`)

Mechanical migration. **Don't touch** `notion_sync/logger.py` or `auth.py` or `app/auth/*.py`.

- [ ] **Step 1: Audit `logging.getLogger` sites**

```bash
grep -nE 'logging\.getLogger|logging\.basicConfig' --include="*.py" -r app/ 2>&1
```

Expected list (already known): 14 files in app/ + main.py = 15 total.

- [ ] **Step 2: In each of the 15 files do this 2-line swap**

Find:
```python
import logging
...
log = logging.getLogger("bridge")
```

Replace `log = logging.getLogger("bridge")` (and add the new import) with:
```python
from app.log import get_logger
log = get_logger("bridge")
```

Keep `import logging` if the file uses `logging.INFO` / `logging.WARNING` etc. elsewhere.

- [ ] **Step 3: `app/main.py` — call `configure()` FIRST in lifespan**

In `app/main.py`:
1. Replace the existing module-top `logging.basicConfig(...)` call with nothing (delete it).
2. Add `from app.log import configure as configure_logging` to the imports.
3. As the FIRST line inside the `lifespan` function body, call `configure_logging()`.

```python
# app/main.py (relevant changes)

from app.log import configure as configure_logging, get_logger  # NEW
# REMOVE: logging.basicConfig(level=logging.INFO, format="...")
log = get_logger("bridge")  # was: log = logging.getLogger("bridge")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    configure_logging()  # NEW — must be FIRST
    state.cwd_root = Path(settings.default_cwd or os.getcwd()).resolve()
    ...
```

- [ ] **Step 4: `app/agent/turn.py` — bind session_id**

Find `current_agent.set(agent)` inside `run_user_turn` and add right after:

```python
from app.log import bind_session  # add to imports near get_logger

async def run_user_turn(agent, text, images=None, files=None):
    images = images or []
    files = files or []
    current_agent.set(agent)
    bind_session(agent.session_id)  # NEW
    async with agent.turn_lock:
        ...
```

- [ ] **Step 5: `app/agent/permission.py` — bind cb_id**

Find `cb_id = secrets.token_urlsafe(8)` and add right after:

```python
from app.log import bind_cb  # add to imports near get_logger

# in can_use_tool, right after cb_id is created:
cb_id = secrets.token_urlsafe(8)
bind_cb(cb_id)  # NEW
fut: asyncio.Future = asyncio.get_running_loop().create_future()
...
```

- [ ] **Step 6: Deploy + verify journal shows JSON**

```bash
deploy  # PowerShell
ssh dashboard-server 'sudo journalctl -u phone-bridge --since "30 sec ago" --no-pager | tail -10'
```

Expected: each log line is JSON like `{"event":"...","log_level":"info","timestamp":"..."}`.

- [ ] **Step 7: Run all tests + smoke**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/ -v 2>&1 | tail -10'
```

Expected: all green (~138 prior + ~38 new from Phase 6a = ~176 total).

```powershell
$env:BASE = "https://dashboard-server.tail4cfa2.ts.net"
$env:BRIDGE_COOKIE = "bridge_session=..."
python tests/smoke_backend.py
```

Expected: 5/5 green.

- [ ] **Step 8: Commit**

```bash
git add app/log.py app/main.py app/agent/*.py app/api/*.py app/integrations/pb/*.py app/reporting/*.py app/ws/*.py
git commit -m "$(cat <<'EOF'
refactor(log): wire structlog across 15 modules + auto-inject session_id / cb_id

Phase 6a Task 6. Mechanical swap: every `log = logging.getLogger("bridge")`
becomes `log = get_logger("bridge")` from app/log.py.

app/main.py lifespan calls configure() FIRST so every subsequent log
line goes through structlog's JSON renderer + contextvars injection.

run_user_turn binds session_id at the top of each turn.
can_use_tool binds cb_id at each gate (so the permission_request flow
is traceable end-to-end via journal grep "session_id" / "cb_id").

notion_sync/logger.py untouched (Phase 5 RotatingFileHandler stays).
auth.py + app/auth/*.py untouched (today's super-link work).

All tests + smoke green post-deploy. journal now shows JSON lines.
EOF
)"
```

## Critical rules for Task 6

- DO NOT touch `notion_sync/logger.py`
- DO NOT touch `auth.py` or `app/auth/*.py`
- DO call `configure()` FIRST in lifespan
- DO confirm journal shows JSON post-deploy

---

## Task 7: Doc cleanup + CHANGELOG + merge

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-06-06-refactor-roadmap.md`

- [ ] **Step 1: CHANGELOG entry**

Insert at top of `CHANGELOG.md` (above the latest 2026-06-09 Phase 5 entry):

```markdown
## 2026-06-10 — Phase 6a · 测试补齐 + structlog + 文档清理

**Branch:** `refactor/phase-6a-tests-structlog`
**实际工时:** 约 X 小时

### 落地的事
- **5 个新单元测试文件 (~38 cases)**:
  - `test_auth_middleware.py` (8) — 今日 super-link 4 paths read-only 覆盖
  - `test_db_crud.py` (11) — session/message/turn CRUD on tmp sqlite
  - `test_build_user_content.py` (8) — xlsx/text/image 上传解析
  - `test_can_use_tool.py` (7) — permission 4 paths
  - `test_run_user_turn.py` (4) — turn pipeline via FakeClient
- **`app/log.py` + structlog 接入**: 15 个模块从 stdlib logging 切到 structlog BoundLogger，每条日志 JSON 化 + contextvars 注入 `session_id` (turn 边界) + `cb_id` (permission gate). systemd journal 现在能 grep 跟踪完整请求链
- **不动今日 super-link 模型**: `app/auth/*` + `auth.py` 全部 read-only

### 闸门
- ✅ ~176 unit tests green (138 prior + ~38 new)
- ✅ smoke 5/5
- ✅ journal JSON 输出，session_id/cb_id 可 grep
- ✅ 今日 super-link 行为未变（test_auth_middleware 8 测覆盖 + 实测可登录）

### 下一步
👉 **Phase 6b** · auth 安全收尾（CSRF / SameSite Strict / Origin 校验）— 每项 acceptance 先讨论再动
👉 **Phase 6c** · OTel hook + request_id 自动注入（与 6b 一起，需 request-id middleware 位置）

---

```

- [ ] **Step 2: Roadmap update**

In `docs/superpowers/specs/2026-06-06-refactor-roadmap.md`, find the Phase 6 row:

```markdown
| 6 收尾 | ⏳ 待开始 | `refactor/phase-6-polish` | — | — | — |
```

Change to:

```markdown
| 6 收尾 | 🚧 6a ✅ / 6b 待开始 | `refactor/phase-6a-tests-structlog` (6a) | 2026-06-10 (6a) | `<6a-merge-SHA>` | CHANGELOG §Phase 6a |
```

Update 下一步入口 to point to Phase 6b discussion.

- [ ] **Step 3: Commit on branch**

```bash
git add CHANGELOG.md docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(changelog): Phase 6a completion report"
```

- [ ] **Step 4: Merge to main**

```bash
git checkout main
git merge --no-ff refactor/phase-6a-tests-structlog -m "Merge branch 'refactor/phase-6a-tests-structlog'

Phase 6a · 测试补齐 + structlog + 文档清理

5 new test files (~38 cases) covering auth_middleware / db CRUD /
content parsing / permission / turn pipeline. structlog adopted
across 15 modules with session_id / cb_id auto-injection. journal
now JSON-per-line.

Today's hidden-auth-superlink work (af00f8f..20e3215) untouched —
Phase 6b (CSRF / SameSite / Origin) is a separate plan needing
explicit per-item user confirmation.

详见 CHANGELOG §Phase 6a。"
git log --oneline -3  # capture merge SHA
```

- [ ] **Step 5: Update roadmap with merge SHA + push**

```bash
# Edit roadmap to fill the actual merge commit SHA
git add docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(roadmap): Phase 6a merged at <SHA>; 6b discussion next"
git push origin main
```

---

## Self-Review

**1. Spec coverage:**

| 路线图 6 项 | Plan task | 状态 |
|---|---|---|
| test_session_manager (已 Phase 3) | — | 已存在，skip |
| test_auth_middleware | Task 1 | ✓ |
| test_run_user_turn (用 fake SDK client) | Task 5 | ✓ |
| test_build_user_content (xlsx/text 解析) | Task 3 | ✓ |
| test_can_use_tool | Task 4 | ✓ |
| test_db_crud | Task 2 | ✓ |
| structlog + contextvars 自动注入 | Tasks 0 + 6 | ✓ (session_id + cb_id) |
| 统一日志出口 from app.log import get_logger | Task 6 | ✓ |
| OTel hook | — | Phase 6c |
| CSRF 双提交 token | — | **Phase 6b** |
| cookie SameSite=Strict | — | **Phase 6b** |
| Origin 校验中间件 | — | **Phase 6b** |
| request_id 注入 | — | Phase 6b/c (需要 middleware 位置) |

✅ Phase 6a 覆盖路线图所有 SAFE 项。

**2. Placeholder scan:**

- Task 5 Step 2 "if FakeClient lacks queue helpers, extend it" — actionable instruction with code snippet, not deferred placeholder.
- Task 6 Steps 4-5 假设 turn.py / permission.py 当前结构 — subagent 先 grep 确认行号。

**3. Type consistency:**

- `configure / get_logger / bind_session / bind_cb` 接口 Task 0 定义，Tasks 5/6 使用，签名一致
- FakeClient queue helpers 在 Task 5 内若扩展，仅扩 fakes/sdk_client.py（test fixture）

**4. Order dependencies:**

- Task 0 (foundation) 必须先
- Tasks 1-5 (tests) 独立，可任意顺序，对 Task 0 弱依赖（不 import app.log）
- Task 6 (wire structlog) 依赖 Task 0；要求 Tasks 1-5 全绿（防止迁移引入 regression）
- Task 7 (doc + merge) 最后

**5. Honest scope:**

- 8 tasks (0-7)
- ~38 new tests + 15 file migrations + 2 docs
- 全程不触碰 `auth.py` / `app/auth/*.py` / `notion_sync/logger.py`
- ~1-2 active days

---

**Plan complete.**
