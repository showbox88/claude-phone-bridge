# Phase 1 · PB Client Unification + MCP Tools Single Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 5 duplicate PocketBase HTTP clients with a single `app/integrations/pb/` package. Add 5xx/429 retry + 401 re-auth. Keep `pb_tools.py` and `mcp_pb/server.py` as thin decorator-only layers (~150 lines each). Eliminate `provisioner.py`'s `pb._http` leakage. Zero behavior change in the happy path; gain rate-limit resilience and rollback-safe side-channel handling.

**Architecture:** `PBClient` (sync, urllib) holds per-instance token cache. `AsyncPBClient` wraps with `asyncio.to_thread`. `refresh_token_into_env(pb)` is a separate side-channel helper for `server.py` (writes `os.environ["PB_TOKEN"/"PB_URL"]` so child Bash subprocesses inherit). `notion_sync/pb_api.PBClient` becomes a shim re-exporting the new class so 19 callers compile unchanged. Tool descriptions move to `app/agent/mcp_tools/prompts.py` as single-source strings.

**Tech Stack:** Python stdlib `urllib.request` (no new HTTP dep). Mocking via thin `urlopen` monkeypatch.

**Branch:** `refactor/phase-1-pb-client` (already created)
**Parent spec:** [2026-06-06-refactor-roadmap.md](../specs/2026-06-06-refactor-roadmap.md) §Phase 1
**Audit basis:** PB client interface inventory completed 2026-06-07.

---

## File Structure

| Path | Action | Purpose |
|---|---|---|
| `app/integrations/__init__.py` | Create | Package marker |
| `app/integrations/pb/__init__.py` | Create | Re-exports public API |
| `app/integrations/pb/client.py` | Create | `PBClient` + `AsyncPBClient` + retry/backoff |
| `app/integrations/pb/exceptions.py` | Create | `PBError` / `PBHTTPError` / `PBAuthError` / `PBNetworkError` |
| `app/integrations/pb/token.py` | Create | `refresh_token_into_env(pb)` side-channel helper |
| `tests/test_pb_client.py` | Create | 12 unit tests via urlopen monkeypatch |
| `notion_sync/pb_api.py` | Modify | Shrink to ≤30 lines: back-compat shim |
| `notion_sync/provisioner.py` | Modify | Replace `pb._http(...)` SLF001 escapes with public collection methods |
| `pocketbase/migrate_notion.py` | Modify | Use new PBClient |
| `mcp_pb/server.py` | Modify | Replace `_http`/`_pb_auth`/`_pb` with PBClient; tool bodies become 1-2 lines |
| `pb_tools.py` | Modify | Replace HTTP/auth block with AsyncPBClient; preserve `_schedule_auto_sync` |
| `server.py` | Modify | `_pb_refresh_token` + `_pb_get_json` delegate to PBClient + `refresh_token_into_env` |
| `app/agent/__init__.py` | Create | Package marker |
| `app/agent/mcp_tools/__init__.py` | Create | Package marker |
| `app/agent/mcp_tools/prompts.py` | Create | Canonical tool descriptions + arg schemas |

**Out of scope for Phase 1**:
- `mcp_pb/server.py` lines 293/301 CSV env reads (`MCP_PB_ALLOWED_HOSTS/ORIGINS`)
- `mcp_pb/server.py` OAuth provider state
- `pb_tools.py` `_schedule_auto_sync` debounce logic (stays in pb_tools.py)
- `todos_client.py` collapse into ops — deferred to Phase 2

---

## API Contract

This is the truth — refer back when writing tests or migrating callers.

### Exceptions

```python
class PBError(Exception): ...

class PBNetworkError(PBError):
    """Network failure after retries exhausted.
    Attrs: method, path, attempts, last_error.
    """

class PBHTTPError(PBError):
    """Non-401 unexpected HTTP status.
    Attrs: code, body (dict|str), method, path.
    """

class PBAuthError(PBError):
    """401 persisted after forced re-auth.
    Attrs: method, path.
    """
```

### Sync client

```python
class PBClient:
    def __init__(self, url: str, email: str, password: str, *,
                 timeout: float = 30.0,
                 retries: int = 3,
                 retry_initial_backoff: float = 1.0,
                 retry_jitter_max: float = 0.5) -> None: ...

    @property
    def url(self) -> str: ...
    @property
    def token(self) -> str | None: ...

    def authenticate(self) -> str: ...
    def request(self, method: str, path: str, *,
                body: dict | None = None, query: dict | None = None,
                timeout: float | None = None,
                retry_on_401: bool = True) -> dict: ...

    # Records
    def list_page(self, collection, *, filter="", sort="-created",
                  expand="", page=1, per_page=30,
                  skip_total=False) -> dict: ...   # envelope
    def list_all(self, collection, *, filter="", sort="-created",
                 expand="", per_page=200) -> list[dict]: ...
    def get_record(self, collection, record_id, *, expand="") -> dict: ...
    def create_record(self, collection, body) -> dict: ...
    def update_record(self, collection, record_id, body) -> dict:   # PATCH
        ...
    def delete_record(self, collection, record_id) -> None: ...

    # Collections
    def list_collections(self) -> list[dict]: ...
    def get_collection(self, name_or_id) -> dict: ...
    def create_collection(self, body) -> dict: ...
    def update_collection(self, name_or_id, body) -> dict: ...
    def delete_collection(self, name_or_id) -> None: ...
```

### Async client

```python
class AsyncPBClient:
    """Wraps PBClient with asyncio.to_thread. Every sync method has an
    async counterpart that to_threads to the sync one."""
```

### Side-channel helper

```python
# app/integrations/pb/token.py
def refresh_token_into_env(pb: PBClient) -> None:
    """Force-authenticate `pb`, mirror token/url into os.environ.
    Child Bash subprocesses inherit PB_TOKEN and PB_URL."""
```

### Retry/backoff policy

- **5xx**: retry up to `retries` times, backoff `initial * 2^(attempt-1)` + uniform jitter `[0, jitter_max]`.
- **429**: same backoff but `wait = max(backoff, Retry-After)`, cap 30s.
- **URLError / socket.timeout**: same as 5xx.
- **401 with `retry_on_401=True`** (default): NOT a retry. Force `authenticate()`, retry exactly once. Persistent 401 → `PBAuthError`.
- **4xx ≠ 401/429**: `PBHTTPError` immediately, no retry.

---

## Task 1: Package scaffold + exceptions

**Files:**
- Create: `app/integrations/__init__.py`
- Create: `app/integrations/pb/__init__.py`
- Create: `app/integrations/pb/exceptions.py`

- [ ] **Step 1: Create empty package markers**

Create `app/integrations/__init__.py`:

```python
"""Third-party / external service integrations.

Created in Phase 1. Hosts client packages: pb (PocketBase), and in
future phases notion, gmail, etc.
"""
```

Create `app/integrations/pb/__init__.py` (partial — only exceptions for now):

```python
"""PocketBase HTTP client + helpers.

Public API (after Phase 1 complete):
    from app.integrations.pb import PBClient, AsyncPBClient
    from app.integrations.pb import PBError, PBHTTPError, PBAuthError, PBNetworkError
    from app.integrations.pb import refresh_token_into_env

Built incrementally; this commit only exposes exceptions. PBClient
and friends land in Tasks 2-3.
"""
from app.integrations.pb.exceptions import (
    PBAuthError,
    PBError,
    PBHTTPError,
    PBNetworkError,
)

__all__ = ["PBAuthError", "PBError", "PBHTTPError", "PBNetworkError"]
```

- [ ] **Step 2: Create exceptions module**

Create `app/integrations/pb/exceptions.py`:

```python
"""Exception hierarchy for the PB client."""
from __future__ import annotations

from typing import Any


class PBError(Exception):
    """Base for all PB client errors."""


class PBNetworkError(PBError):
    """Network failure after exhausting retries.

    Attributes:
        method, path: of the failing request.
        attempts: number of attempts before giving up.
        last_error: final underlying exception (URLError, etc.).
    """
    def __init__(self, method: str, path: str, attempts: int,
                 last_error: Exception):
        super().__init__(
            f"PB {method} {path}: network failure after {attempts} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        )
        self.method = method
        self.path = path
        self.attempts = attempts
        self.last_error = last_error


class PBHTTPError(PBError):
    """Non-401 unexpected HTTP status from PB.

    Attributes:
        code: HTTP status code.
        body: parsed JSON if possible, raw text otherwise.
        method, path: of the failing request.
    """
    def __init__(self, code: int, body: Any, method: str, path: str):
        msg = (
            body.get("message") if isinstance(body, dict) else None
        ) or (
            body.get("error") if isinstance(body, dict) else None
        ) or (body if isinstance(body, str) else "")
        super().__init__(f"PB {method} {path}: HTTP {code} — {msg or body!r}")
        self.code = code
        self.body = body
        self.method = method
        self.path = path


class PBAuthError(PBError):
    """401 persisted after a forced re-auth attempt."""
    def __init__(self, method: str, path: str):
        super().__init__(
            f"PB {method} {path}: 401 persisted after forced re-auth"
        )
        self.method = method
        self.path = path
```

- [ ] **Step 3: Sanity**

```bash
python -c "from app.integrations.pb.exceptions import PBHTTPError; e = PBHTTPError(404, {'message': 'not found'}, 'GET', '/api/x'); print(e)"
```

Expected: `PB GET /api/x: HTTP 404 — not found`.

- [ ] **Step 4: Commit**

```bash
git add app/integrations/__init__.py app/integrations/pb/__init__.py app/integrations/pb/exceptions.py
git commit -m "refactor(pb): add app/integrations/pb scaffold + exceptions

PBError hierarchy: PBHTTPError (code/body/method/path), PBAuthError
(401 after forced re-auth), PBNetworkError (after retries exhausted).
__init__ only re-exports exceptions for now; PBClient and friends
land in Task 2-3."
```

---

## Task 2: PBClient + 12 unit tests

**Files:**
- Create: `app/integrations/pb/client.py`
- Create: `tests/test_pb_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pb_client.py`:

```python
"""Tests for app.integrations.pb.PBClient.

Stdlib-only: monkey-patches the client's _urlopen to inject responses.
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.pb.client import PBClient
from app.integrations.pb.exceptions import (
    PBAuthError,
    PBHTTPError,
    PBNetworkError,
)


class _MockResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._payload = json.dumps(body).encode() if isinstance(body, dict) else body
        self.headers = headers or {}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _MockQueue:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def urlopen(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        try:
            body = json.loads(req.data) if req.data else None
        except (ValueError, TypeError):
            body = req.data
        self.calls.append((method, url, body))

        if not self.responses:
            raise RuntimeError(f"unexpected request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _make_client(responses):
    queue = _MockQueue(responses)
    pb = PBClient(
        "http://localhost:8090", "admin@x.com", "pw",
        retries=2, retry_initial_backoff=0.0, retry_jitter_max=0.0,
        timeout=5.0,
    )
    pb._urlopen = queue.urlopen
    return pb, queue


def test_authenticate_and_get_record():
    auth = _MockResponse(200, {"token": "tok-1"})
    got = _MockResponse(200, {"id": "abc", "title": "Hello"})
    pb, q = _make_client([auth, got])

    result = pb.get_record("notes", "abc")

    assert result == {"id": "abc", "title": "Hello"}
    assert len(q.calls) == 2
    assert q.calls[0][0] == "POST"
    assert q.calls[0][1].endswith("/api/collections/_superusers/auth-with-password")
    assert q.calls[1][0] == "GET"
    assert q.calls[1][1].endswith("/api/collections/notes/records/abc")


def test_401_triggers_forced_reauth_and_one_retry():
    auth1 = _MockResponse(200, {"token": "tok-stale"})
    err_401 = urllib.error.HTTPError(
        "http://x", 401, "Unauthorized",
        {}, io.BytesIO(b'{"message": "token expired"}')
    )
    auth2 = _MockResponse(200, {"token": "tok-fresh"})
    got = _MockResponse(200, {"id": "abc"})
    pb, q = _make_client([auth1, err_401, auth2, got])

    result = pb.get_record("notes", "abc")
    assert result == {"id": "abc"}
    assert len(q.calls) == 4
    assert pb.token == "tok-fresh"


def test_persistent_401_raises_pbautherror():
    auth1 = _MockResponse(200, {"token": "tok"})
    err_a = urllib.error.HTTPError("http://x", 401, "u", {}, io.BytesIO(b'{}'))
    auth2 = _MockResponse(200, {"token": "tok2"})
    err_b = urllib.error.HTTPError("http://x", 401, "u", {}, io.BytesIO(b'{}'))
    pb, q = _make_client([auth1, err_a, auth2, err_b])

    try:
        pb.get_record("notes", "abc")
    except PBAuthError:
        return
    raise AssertionError("expected PBAuthError")


def test_5xx_retries_then_succeeds():
    auth = _MockResponse(200, {"token": "tok"})
    e500 = urllib.error.HTTPError("http://x", 500, "s", {}, io.BytesIO(b'{}'))
    e502 = urllib.error.HTTPError("http://x", 502, "b", {}, io.BytesIO(b'{}'))
    ok = _MockResponse(200, {"id": "abc"})
    pb, q = _make_client([auth, e500, e502, ok])

    result = pb.get_record("notes", "abc")
    assert result == {"id": "abc"}
    assert len(q.calls) == 4


def test_5xx_exhausts_retries_then_raises():
    auth = _MockResponse(200, {"token": "tok"})
    err = lambda: urllib.error.HTTPError("http://x", 500, "s", {}, io.BytesIO(b'{}'))
    pb, q = _make_client([auth, err(), err(), err()])

    try:
        pb.get_record("notes", "abc")
    except PBHTTPError as e:
        assert e.code == 500
        return
    raise AssertionError("expected PBHTTPError")


def test_429_honors_retry_after_header():
    auth = _MockResponse(200, {"token": "tok"})
    e429 = urllib.error.HTTPError(
        "http://x", 429, "tm",
        {"Retry-After": "0"}, io.BytesIO(b'{}'),
    )
    ok = _MockResponse(200, {"id": "abc"})
    pb, q = _make_client([auth, e429, ok])

    t0 = time.monotonic()
    result = pb.get_record("notes", "abc")
    elapsed = time.monotonic() - t0
    assert result == {"id": "abc"}
    assert elapsed < 0.5


def test_4xx_other_than_401_raises_immediately():
    auth = _MockResponse(200, {"token": "tok"})
    e404 = urllib.error.HTTPError(
        "http://x", 404, "nf", {},
        io.BytesIO(b'{"message": "not found"}'),
    )
    pb, q = _make_client([auth, e404])

    try:
        pb.get_record("notes", "abc")
    except PBHTTPError as e:
        assert e.code == 404
        assert "not found" in str(e)
        assert len(q.calls) == 2
        return
    raise AssertionError("expected PBHTTPError")


def test_network_error_retries_then_raises():
    auth = _MockResponse(200, {"token": "tok"})
    pb, q = _make_client([
        auth,
        urllib.error.URLError("connection refused"),
        urllib.error.URLError("connection refused"),
        urllib.error.URLError("connection refused"),
    ])

    try:
        pb.get_record("notes", "abc")
    except PBNetworkError as e:
        assert e.attempts == 2
        return
    raise AssertionError("expected PBNetworkError")


def test_list_page_returns_envelope():
    auth = _MockResponse(200, {"token": "tok"})
    page = _MockResponse(200, {
        "items": [{"id": "1"}, {"id": "2"}],
        "page": 1, "perPage": 2, "totalPages": 3, "totalItems": 5,
    })
    pb, q = _make_client([auth, page])

    result = pb.list_page("notes", page=1, per_page=2)
    assert result["items"] == [{"id": "1"}, {"id": "2"}]
    assert result["totalPages"] == 3


def test_list_all_paginates():
    auth = _MockResponse(200, {"token": "tok"})
    p1 = _MockResponse(200, {
        "items": [{"id": "1"}, {"id": "2"}],
        "page": 1, "perPage": 2, "totalPages": 2, "totalItems": 3,
    })
    p2 = _MockResponse(200, {
        "items": [{"id": "3"}],
        "page": 2, "perPage": 2, "totalPages": 2, "totalItems": 3,
    })
    pb, q = _make_client([auth, p1, p2])

    result = pb.list_all("notes", per_page=2)
    assert [r["id"] for r in result] == ["1", "2", "3"]


def test_create_update_delete_record():
    auth = _MockResponse(200, {"token": "tok"})
    created = _MockResponse(200, {"id": "abc", "title": "A"})
    updated = _MockResponse(200, {"id": "abc", "title": "B"})
    deleted = _MockResponse(204, b"")
    pb, q = _make_client([auth, created, updated, deleted])

    c = pb.create_record("notes", {"title": "A"})
    assert c["title"] == "A"
    u = pb.update_record("notes", "abc", {"title": "B"})
    assert u["title"] == "B"
    pb.delete_record("notes", "abc")
    methods = [call[0] for call in q.calls]
    assert methods == ["POST", "POST", "PATCH", "DELETE"]


def test_collection_crud():
    auth = _MockResponse(200, {"token": "tok"})
    listing = _MockResponse(200, {
        "items": [{"name": "x"}],
        "page": 1, "perPage": 200, "totalPages": 1, "totalItems": 1,
    })
    got = _MockResponse(200, {"id": "id1", "name": "x"})
    created = _MockResponse(200, {"id": "id2", "name": "y"})
    updated = _MockResponse(200, {"id": "id1", "name": "x", "system": False})
    deleted = _MockResponse(204, b"")
    pb, q = _make_client([auth, listing, got, created, updated, deleted])

    cols = pb.list_collections()
    assert cols == [{"name": "x"}]
    assert pb.get_collection("x") == {"id": "id1", "name": "x"}
    assert pb.create_collection({"name": "y", "type": "base"})["id"] == "id2"
    assert pb.update_collection("x", {"system": False})["system"] is False
    pb.delete_collection("x")
    methods = [call[0] for call in q.calls]
    assert methods == ["POST", "GET", "GET", "POST", "PATCH", "DELETE"]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
```

- [ ] **Step 2: Run tests to verify failure**

```bash
python tests/test_pb_client.py 2>&1 | tail -3
```

Expected: `ModuleNotFoundError: No module named 'app.integrations.pb.client'`.

- [ ] **Step 3: Write the implementation**

Create `app/integrations/pb/client.py`:

```python
"""PocketBase HTTP client.

Sync core (`PBClient`); async wrapper (`AsyncPBClient`) uses to_thread.

Design:
- Per-instance token cache (no module globals).
- urllib.request only — no new HTTP deps.
- request() is the single HTTP entry point; CRUD helpers call it.
- 401 → forced authenticate() + one re-request, not counted as retry.
- 5xx / 429 / network → exponential backoff with jitter, capped retries.
- 4xx ≠ 401/429 → PBHTTPError, no retry.

Tests inject a mock urlopen via `pb._urlopen = mock_callable`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from app.integrations.pb.exceptions import (
    PBAuthError,
    PBHTTPError,
    PBNetworkError,
)

log = logging.getLogger("app.pb.client")

_AUTH_PATH = "/api/collections/_superusers/auth-with-password"
_RETRY_AFTER_CAP_SECS = 30.0


class PBClient:
    def __init__(
        self,
        url: str,
        email: str,
        password: str,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        retry_initial_backoff: float = 1.0,
        retry_jitter_max: float = 0.5,
    ) -> None:
        self._url = url.rstrip("/")
        self._email = email
        self._password = password
        self._timeout = timeout
        self._retries = retries
        self._retry_initial_backoff = retry_initial_backoff
        self._retry_jitter_max = retry_jitter_max
        self._token: str | None = None
        self._urlopen: Callable = urllib.request.urlopen

    @property
    def url(self) -> str:
        return self._url

    @property
    def token(self) -> str | None:
        return self._token

    def authenticate(self) -> str:
        body = {"identity": self._email, "password": self._password}
        result = self._request_raw(
            "POST", _AUTH_PATH, body=body,
            with_auth=False, retry_on_401=False,
        )
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise PBAuthError("POST", _AUTH_PATH)
        self._token = token
        return token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        query: dict | None = None,
        timeout: float | None = None,
        retry_on_401: bool = True,
    ) -> dict:
        if not self._token:
            self.authenticate()
        return self._request_raw(
            method, path, body=body, query=query,
            timeout=timeout, with_auth=True,
            retry_on_401=retry_on_401,
        )

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        query: dict | None = None,
        timeout: float | None = None,
        with_auth: bool,
        retry_on_401: bool,
    ) -> dict:
        url = self._url + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)

        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if with_auth and self._token:
            headers["Authorization"] = self._token

        data = json.dumps(body).encode() if body is not None else None
        effective_timeout = timeout if timeout is not None else self._timeout

        last_net_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            req = urllib.request.Request(
                url, data=data, method=method, headers=headers,
            )
            try:
                resp = self._urlopen(req, timeout=effective_timeout)
                with resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return {"_raw": raw.decode("utf-8", errors="replace")}
            except urllib.error.HTTPError as e:
                try:
                    err_raw = e.read()
                    err_body = json.loads(err_raw) if err_raw else {}
                except (json.JSONDecodeError, ValueError):
                    err_body = err_raw.decode("utf-8", errors="replace") if err_raw else ""
                except Exception:
                    err_body = ""

                if e.code == 401:
                    if not retry_on_401:
                        raise PBAuthError(method, path) from e
                    log.warning("PB %s %s: 401 — forcing re-auth", method, path)
                    self.authenticate()
                    if with_auth:
                        headers["Authorization"] = self._token or ""
                    return self._request_raw(
                        method, path, body=body, query=query,
                        timeout=timeout, with_auth=with_auth,
                        retry_on_401=False,
                    )

                if e.code == 429 or 500 <= e.code < 600:
                    wait = self._backoff_secs(attempt)
                    if e.code == 429:
                        retry_after = e.headers.get("Retry-After") if e.headers else None
                        try:
                            ra = float(retry_after) if retry_after else 0
                        except ValueError:
                            ra = 0
                        wait = min(max(wait, ra), _RETRY_AFTER_CAP_SECS)
                    log.warning(
                        "PB %s %s: HTTP %s, retry %d/%d after %.2fs",
                        method, path, e.code, attempt, self._retries, wait,
                    )
                    if attempt >= self._retries:
                        log.error(
                            "PB %s %s: HTTP %s after %d attempts",
                            method, path, e.code, attempt,
                        )
                        raise PBHTTPError(e.code, err_body, method, path) from e
                    time.sleep(wait)
                    continue

                raise PBHTTPError(e.code, err_body, method, path) from e

            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                last_net_error = e
                if attempt >= self._retries:
                    log.error(
                        "PB %s %s: %s after %d attempts",
                        method, path, type(e).__name__, attempt,
                    )
                    raise PBNetworkError(method, path, attempt, e) from e
                wait = self._backoff_secs(attempt)
                log.warning(
                    "PB %s %s: %s, retry %d/%d after %.2fs",
                    method, path, type(e).__name__, attempt, self._retries, wait,
                )
                time.sleep(wait)
                continue

        raise PBNetworkError(method, path, self._retries,
                             last_net_error or Exception("unknown"))

    def _backoff_secs(self, attempt: int) -> float:
        base = self._retry_initial_backoff * (2 ** (attempt - 1))
        jitter = random.uniform(0, self._retry_jitter_max)
        return base + jitter

    # --- Records --------------------------------------------------------

    def list_page(
        self, collection: str, *, filter: str = "",
        sort: str = "-created", expand: str = "",
        page: int = 1, per_page: int = 30,
        skip_total: bool = False,
    ) -> dict:
        query: dict = {"page": page, "perPage": per_page}
        if filter:
            query["filter"] = filter
        if sort:
            query["sort"] = sort
        if expand:
            query["expand"] = expand
        if skip_total:
            query["skipTotal"] = "true"
        return self.request(
            "GET", f"/api/collections/{collection}/records", query=query,
        )

    def list_all(
        self, collection: str, *, filter: str = "",
        sort: str = "-created", expand: str = "",
        per_page: int = 200,
    ) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            envelope = self.list_page(
                collection, filter=filter, sort=sort, expand=expand,
                page=page, per_page=per_page,
            )
            items.extend(envelope.get("items", []))
            total_pages = envelope.get("totalPages", 1)
            if page >= total_pages:
                return items
            page += 1

    def get_record(self, collection: str, record_id: str, *,
                   expand: str = "") -> dict:
        query = {"expand": expand} if expand else None
        return self.request(
            "GET",
            f"/api/collections/{collection}/records/{record_id}",
            query=query,
        )

    def create_record(self, collection: str, body: dict) -> dict:
        return self.request(
            "POST", f"/api/collections/{collection}/records", body=body,
        )

    def update_record(self, collection: str, record_id: str,
                      body: dict) -> dict:
        return self.request(
            "PATCH",
            f"/api/collections/{collection}/records/{record_id}",
            body=body,
        )

    def delete_record(self, collection: str, record_id: str) -> None:
        self.request(
            "DELETE",
            f"/api/collections/{collection}/records/{record_id}",
        )

    # --- Collections ----------------------------------------------------

    def list_collections(self) -> list[dict]:
        envelope = self.request(
            "GET", "/api/collections",
            query={"page": 1, "perPage": 200, "skipTotal": "true"},
        )
        return envelope.get("items", [])

    def get_collection(self, name_or_id: str) -> dict:
        return self.request(
            "GET", f"/api/collections/{name_or_id}",
        )

    def create_collection(self, body: dict) -> dict:
        return self.request("POST", "/api/collections", body=body)

    def update_collection(self, name_or_id: str, body: dict) -> dict:
        return self.request(
            "PATCH", f"/api/collections/{name_or_id}", body=body,
        )

    def delete_collection(self, name_or_id: str) -> None:
        self.request("DELETE", f"/api/collections/{name_or_id}")


class AsyncPBClient:
    """Wraps PBClient with asyncio.to_thread."""

    def __init__(self, *args, **kwargs) -> None:
        self._sync = PBClient(*args, **kwargs)

    @property
    def url(self) -> str:
        return self._sync.url

    @property
    def token(self) -> str | None:
        return self._sync.token

    async def authenticate(self) -> str:
        return await asyncio.to_thread(self._sync.authenticate)

    async def request(self, method: str, path: str, **kw) -> dict:
        return await asyncio.to_thread(self._sync.request, method, path, **kw)

    async def list_page(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.list_page, *args, **kwargs)

    async def list_all(self, *args, **kwargs) -> list[dict]:
        return await asyncio.to_thread(self._sync.list_all, *args, **kwargs)

    async def get_record(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.get_record, *args, **kwargs)

    async def create_record(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.create_record, *args, **kwargs)

    async def update_record(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.update_record, *args, **kwargs)

    async def delete_record(self, *args, **kwargs) -> None:
        await asyncio.to_thread(self._sync.delete_record, *args, **kwargs)

    async def list_collections(self) -> list[dict]:
        return await asyncio.to_thread(self._sync.list_collections)

    async def get_collection(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.get_collection, *args, **kwargs)

    async def create_collection(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.create_collection, *args, **kwargs)

    async def update_collection(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.update_collection, *args, **kwargs)

    async def delete_collection(self, *args, **kwargs) -> None:
        await asyncio.to_thread(self._sync.delete_collection, *args, **kwargs)
```

- [ ] **Step 4: Run tests**

```bash
python tests/test_pb_client.py
```

Expected: `12/12 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/pb/client.py tests/test_pb_client.py
git commit -m "refactor(pb): add PBClient + AsyncPBClient with retry/backoff

Sync core: per-instance token cache, 401 forced re-auth + one retry
(not counted toward retry budget), 5xx/429/network retries with
exponential backoff + jitter, 429 honors Retry-After up to 30s cap,
4xx other than 401/429 raises immediately.

Async wrapper: to_thread-based, no new HTTP dep.

12 unit tests via urlopen monkeypatch (stdlib-only)."
```

---

## Task 3: refresh_token_into_env helper

**Files:**
- Create: `app/integrations/pb/token.py`
- Modify: `app/integrations/pb/__init__.py`

- [ ] **Step 1: Create the helper**

Create `app/integrations/pb/token.py`:

```python
"""Side-channel helper that mirrors PB's token into os.environ.

Used by `server.py` because child Bash subprocesses spawned by Claude
SDK inherit env vars; the CHECKIN flow uses `$PB_TOKEN` and `$PB_URL`
directly in curl commands.

The 12h refresh loop in server.py calls this on schedule; the 401
fallback inside PBClient also handles token expiry, but the env
mirror only happens when refresh_token_into_env is explicitly called.
"""
from __future__ import annotations

import logging
import os

from app.integrations.pb.client import PBClient

log = logging.getLogger("app.pb.token")


def refresh_token_into_env(pb: PBClient) -> None:
    """Force-authenticate `pb` and mirror token/url into os.environ.

    Subsequent child processes inherit:
        PB_TOKEN  — current PB admin token
        PB_URL    — the PB base URL `pb` is configured for

    Raises PBAuthError on credential failure.
    """
    token = pb.authenticate()
    os.environ["PB_TOKEN"] = token
    os.environ["PB_URL"] = pb.url
    log.info("PB token refreshed (len=%d)", len(token))
```

- [ ] **Step 2: Update __init__.py to export everything**

Replace `app/integrations/pb/__init__.py`:

```python
"""PocketBase HTTP client + helpers.

Public API:
    from app.integrations.pb import PBClient, AsyncPBClient
    from app.integrations.pb import PBError, PBHTTPError, PBAuthError, PBNetworkError
    from app.integrations.pb import refresh_token_into_env
"""
from app.integrations.pb.client import AsyncPBClient, PBClient
from app.integrations.pb.exceptions import (
    PBAuthError,
    PBError,
    PBHTTPError,
    PBNetworkError,
)
from app.integrations.pb.token import refresh_token_into_env

__all__ = [
    "AsyncPBClient",
    "PBAuthError",
    "PBClient",
    "PBError",
    "PBHTTPError",
    "PBNetworkError",
    "refresh_token_into_env",
]
```

- [ ] **Step 3: Sanity**

```bash
python -c "from app.integrations.pb import PBClient, AsyncPBClient, refresh_token_into_env, PBError; print('OK')"
```

Expected: `OK`.

- [ ] **Step 4: Test side-channel manually**

```bash
python -c "
import os
os.environ.pop('PB_TOKEN', None)
os.environ.pop('PB_URL', None)
from app.integrations.pb import PBClient, refresh_token_into_env
class _R:
    status = 200
    def read(self): import json; return json.dumps({'token': 'side-tok'}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): pass
pb = PBClient('http://localhost:8090', 'e', 'p')
pb._urlopen = lambda req, timeout=None: _R()
refresh_token_into_env(pb)
print('PB_TOKEN:', os.environ['PB_TOKEN'])
print('PB_URL:', os.environ['PB_URL'])
"
```

Expected:
```
PB_TOKEN: side-tok
PB_URL: http://localhost:8090
```

- [ ] **Step 5: Commit**

```bash
git add app/integrations/pb/token.py app/integrations/pb/__init__.py
git commit -m "refactor(pb): add refresh_token_into_env side-channel helper

For server.py's child-Bash subprocess contract: PB_TOKEN and PB_URL
get written to os.environ so curl in CHECKIN scripts inherits them.
Public API fully exposed via app.integrations.pb."
```

---

## Task 4: Migrate `pocketbase/migrate_notion.py` (lowest risk)

**Files:**
- Modify: `pocketbase/migrate_notion.py`

- [ ] **Step 1: Find the PB-touching surface**

```bash
grep -nE "^def pb|^def http|^def pb_token|PB_URL\s*=|PB_EMAIL\s*=|PB_PASSWORD\s*=" pocketbase/migrate_notion.py
```

Note the line ranges of the existing PB block (typically a 70-line region).

- [ ] **Step 2: Replace the PB section**

Read 10 lines of context around `def pb(` and the helpers below. Replace
the whole PB section (PB_URL constants + pb_token + http used for PB +
pb wrapper) with:

```python
# ---------- PB ----------
# Phase 1: use the unified app.integrations.pb client. This script is
# a completed one-shot migration kept for reference.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.pb import PBClient  # noqa: E402
from app.settings import settings  # noqa: E402

_pb_client: PBClient | None = None


def _pb() -> PBClient:
    global _pb_client
    if _pb_client is None:
        _pb_client = PBClient(
            settings.pocketbase_url,
            settings.pocketbase_admin_email,
            settings.pocketbase_admin_password,
        )
    return _pb_client


def pb(method: str, path: str, body: dict | None = None) -> dict:
    """Back-compat wrapper for the script's existing call sites."""
    return _pb().request(method, path, body=body)
```

Confirm Notion helpers (`notion_get` / `notion_post`) are untouched —
they talk to Notion, not PB.

- [ ] **Step 3: Parse check**

```bash
python -c "import ast; ast.parse(open('pocketbase/migrate_notion.py', encoding='utf-8').read()); print('OK')"
```

Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add pocketbase/migrate_notion.py
git commit -m "refactor(pb): migrate migrate_notion.py to app.integrations.pb

One-shot migration script swapped to PBClient. Lowest-risk validation
that the new client handles script-style usage."
```

---

## Task 5: Migrate `notion_sync/pb_api.py` (shim)

**Files:**
- Modify: `notion_sync/pb_api.py`

- [ ] **Step 1: Read current surface**

```bash
grep -nE "^class PBClient|^    def " notion_sync/pb_api.py
```

Expected methods: `__init__`, `_http`, `list_records`, `get_record`,
`create_record`, `update_record`, `delete_record`, `list_collections`.

- [ ] **Step 2: Replace whole file**

Replace `notion_sync/pb_api.py`:

```python
"""Back-compat shim — the real PBClient now lives in app.integrations.pb.

This module exists so the 19 existing call sites (in notion_sync/,
server.py REST handlers, and scripts/) can keep `from notion_sync.pb_api
import PBClient` working unchanged. Phase 2 (server.py decomposition)
removes the shim.

Notable behavior:
  - list_records preserved (auto-paginates -> list[dict]); maps to
    app.integrations.pb.PBClient.list_all.
  - update_record uses PATCH (unchanged).
  - No-arg constructor reads settings (was already the case).

If you're writing NEW code: import from app.integrations.pb instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.pb import PBClient as _UnifiedPBClient  # noqa: E402
from app.settings import Settings  # noqa: E402


class PBClient(_UnifiedPBClient):
    """Back-compat: zero-arg constructor reads from Settings()."""

    def __init__(self) -> None:
        s = Settings()
        super().__init__(
            s.pocketbase_url,
            s.pocketbase_admin_email,
            s.pocketbase_admin_password,
        )

    def list_records(
        self, collection: str, *, filter: str = "",
        sort: str = "-created", per_page: int = 200,
    ) -> list[dict]:
        """Auto-paginating list, preserving the legacy signature."""
        return self.list_all(
            collection, filter=filter, sort=sort, per_page=per_page,
        )
```

- [ ] **Step 3: Sanity**

```bash
python -c "
from notion_sync.pb_api import PBClient
pb = PBClient.__new__(PBClient)
for name in ['list_records', 'get_record', 'create_record', 'update_record',
              'delete_record', 'list_collections']:
    assert hasattr(pb, name), f'missing {name}'
print('OK')
"
```

Expected: `OK`.

- [ ] **Step 4: Tests**

```bash
python -m pytest tests/notion_sync/ 2>&1 | tail -5
```

Expected: 106 pass + 1 pre-existing test_icons fail.

- [ ] **Step 5: Commit**

```bash
git add notion_sync/pb_api.py
git commit -m "refactor(pb): notion_sync/pb_api.py becomes a thin shim

PBClient now inherits from app.integrations.pb.PBClient. Zero-arg
constructor + list_records (auto-paginate signature) preserved so
19 existing callers stay unchanged."
```

---

## Task 6: Fix `notion_sync/provisioner.py` SLF001 escapes

**Files:**
- Modify: `notion_sync/provisioner.py`

- [ ] **Step 1: Find offending lines**

```bash
grep -nE "pb\._http|noqa: SLF001" notion_sync/provisioner.py
```

- [ ] **Step 2: Replace each `pb._http(...)` with the public method**

Use this mapping:

| Current call | Replacement |
|---|---|
| `pb._http("POST", "/api/collections", body=X)` | `pb.create_collection(X)` |
| `pb._http("PATCH", "/api/collections/Y", body=X)` | `pb.update_collection("Y", X)` |
| `pb._http("DELETE", "/api/collections/Y")` | `pb.delete_collection("Y")` |
| `pb._http("GET", "/api/collections/Y")` | `pb.get_collection("Y")` |

Read 3 lines of context around each call to confirm. Remove `# noqa: SLF001`.

- [ ] **Step 3: Verify no SLF001 remains**

```bash
grep -nE "pb\._http|noqa: SLF001" notion_sync/provisioner.py
```

Expected: no matches.

- [ ] **Step 4: Tests**

```bash
python -m pytest tests/notion_sync/test_provisioner.py 2>&1 | tail -5
```

Expected: 13/13 pass.

- [ ] **Step 5: Commit**

```bash
git add notion_sync/provisioner.py
git commit -m "refactor(pb): provisioner uses public collection methods

Replaces pb._http() SLF001 escapes with create_collection /
update_collection / delete_collection."
```

---

## Task 7: MCP tool prompts (single source)

**Files:**
- Create: `app/agent/__init__.py`
- Create: `app/agent/mcp_tools/__init__.py`
- Create: `app/agent/mcp_tools/prompts.py`

- [ ] **Step 1: Create the agent packages**

Create `app/agent/__init__.py`:

```python
"""Claude SDK / MCP agent surface.

Created in Phase 1 for shared MCP tool definitions. Phase 2 will move
agent options, permission, content-building, and turn-running here.
"""
```

Create `app/agent/mcp_tools/__init__.py`:

```python
"""MCP tool definitions shared between in-process (pb_tools.py) and
external (mcp_pb/server.py) MCP servers.

Public:
    from app.agent.mcp_tools.prompts import TOOL_DESCRIPTIONS, TOOL_ARG_SCHEMAS
"""
```

- [ ] **Step 2: Capture current descriptions from pb_tools.py**

```bash
grep -A2 "@tool(" pb_tools.py | head -80
```

Copy the exact (name, description) tuples for each of the 10 PB tools.

- [ ] **Step 3: Write canonical prompts file**

Create `app/agent/mcp_tools/prompts.py`. Use the actual descriptions
captured in Step 2 verbatim. Below is a sample structure — REPLACE the
description strings with the actual pb_tools.py text:

```python
"""Canonical tool descriptions for the PB MCP surface.

Both in-process (pb_tools.py with claude_agent_sdk's @tool) and external
(mcp_pb/server.py with FastMCP @mcp.tool()) servers source description
strings from this module.

To update a description: edit here; both servers pick it up next deploy.
"""
from __future__ import annotations

from typing import TypedDict


class ArgSpec(TypedDict, total=False):
    type: str
    description: str
    required: bool
    default: object


# REPLACE each value with the verbatim string from pb_tools.py
TOOL_DESCRIPTIONS: dict[str, str] = {
    "pb_list_collections": "...",
    "pb_search": "...",
    "pb_get": "...",
    "pb_get_collection": "...",
    "pb_create": "...",
    "pb_update": "...",
    "pb_delete": "...",
    "pb_create_collection": "...",
    "pb_update_collection": "...",
    "pb_delete_collection": "...",
    "smartnote_open_context": "...",
}


TOOL_ARG_SCHEMAS: dict[str, dict[str, ArgSpec]] = {
    "pb_list_collections": {},
    "pb_search": {
        "collection": {"type": "string", "description": "Collection name", "required": True},
        "filter": {"type": "string", "description": "PB filter expression", "required": False, "default": ""},
        "sort": {"type": "string", "description": "Sort spec, e.g. '-created'", "required": False, "default": "-created"},
        "expand": {"type": "string", "description": "Relation expand, comma-separated", "required": False, "default": ""},
        "page": {"type": "integer", "description": "1-indexed page", "required": False, "default": 1},
        "per_page": {"type": "integer", "description": "Page size (max 200)", "required": False, "default": 30},
    },
    "pb_get": {
        "collection": {"type": "string", "description": "Collection name", "required": True},
        "id": {"type": "string", "description": "Record id", "required": True},
        "expand": {"type": "string", "description": "Relation expand", "required": False, "default": ""},
    },
    "pb_get_collection": {
        "name_or_id": {"type": "string", "description": "Collection name or id", "required": True},
    },
    "pb_create": {
        "collection": {"type": "string", "description": "Collection name", "required": True},
        "body": {"type": "object", "description": "Record fields", "required": True},
    },
    "pb_update": {
        "collection": {"type": "string", "description": "Collection name", "required": True},
        "id": {"type": "string", "description": "Record id", "required": True},
        "body": {"type": "object", "description": "Fields to patch", "required": True},
    },
    "pb_delete": {
        "collection": {"type": "string", "description": "Collection name", "required": True},
        "id": {"type": "string", "description": "Record id", "required": True},
    },
    "pb_create_collection": {
        "body": {"type": "object", "description": "Full collection schema", "required": True},
    },
    "pb_update_collection": {
        "name_or_id": {"type": "string", "description": "Collection name or id", "required": True},
        "body": {"type": "object", "description": "Schema patch", "required": True},
    },
    "pb_delete_collection": {
        "name_or_id": {"type": "string", "description": "Collection name or id", "required": True},
    },
    "smartnote_open_context": {},
}
```

Compare arg schemas with the current `@tool(...)` 3rd arg in pb_tools.py;
match the parameter names exactly.

- [ ] **Step 4: Sanity**

```bash
python -c "from app.agent.mcp_tools.prompts import TOOL_DESCRIPTIONS; print('canonical tool count:', len(TOOL_DESCRIPTIONS))"
```

Expected: `canonical tool count: 11`.

- [ ] **Step 5: Commit**

```bash
git add app/agent/__init__.py app/agent/mcp_tools/__init__.py app/agent/mcp_tools/prompts.py
git commit -m "refactor(agent): centralize PB MCP tool descriptions + arg schemas

app/agent/mcp_tools/prompts.py is the single source for tool
descriptions used by both in-process (pb_tools.py) and external
(mcp_pb/server.py) MCP servers."
```

---

## Task 8: Migrate `mcp_pb/server.py` to PBClient

**Files:**
- Modify: `mcp_pb/server.py`

- [ ] **Step 1: Find existing HTTP/auth block**

```bash
grep -nE "^def _pb_auth|^def _http|^def _pb\b|^_pb_token\b" mcp_pb/server.py
```

- [ ] **Step 2: Replace HTTP/auth block with PBClient lazy-init**

Find the comment block (`# ---------- PocketBase HTTP -----------` or
similar) and the `_http` / `_pb_auth` / `_pb` definitions. Replace with:

```python
# ---------- PocketBase HTTP ----------
# Phase 1: replaced bespoke _http/_pb_auth/_pb with the unified
# app.integrations.pb.PBClient.

from app.integrations.pb import PBClient  # noqa: E402

_pb_client: PBClient | None = None


def _pb() -> PBClient:
    global _pb_client
    if _pb_client is None:
        _pb_client = PBClient(PB_URL, PB_EMAIL, PB_PASSWORD)
    return _pb_client
```

(PB_URL, PB_EMAIL, PB_PASSWORD already come from settings via Phase 0.)

- [ ] **Step 3: Rewrite each @mcp.tool() body**

For each of the 10 PB tools, replace the body with a 1-2 line PBClient
call using the mapping:

| Tool | Body |
|---|---|
| `pb_list_collections` | `return {"items": _pb().list_collections()}` |
| `pb_search(collection, filter="", sort="-created", expand="", page=1, per_page=30)` | `return _pb().list_page(collection, filter=filter, sort=sort, expand=expand, page=page, per_page=per_page)` |
| `pb_get(collection, id, expand="")` | `return _pb().get_record(collection, id, expand=expand)` |
| `pb_get_collection(name_or_id)` | `return _pb().get_collection(name_or_id)` |
| `pb_create(collection, body)` | `return _pb().create_record(collection, body)` |
| `pb_update(collection, id, body)` | `return _pb().update_record(collection, id, body)` |
| `pb_delete(collection, id)` | `_pb().delete_record(collection, id); return {"ok": True}` |
| `pb_create_collection(body)` | `return _pb().create_collection(body)` |
| `pb_update_collection(name_or_id, body)` | `return _pb().update_collection(name_or_id, body)` |
| `pb_delete_collection(name_or_id)` | `_pb().delete_collection(name_or_id); return {"ok": True}` |

If `smartnote_open_context` exists, leave its logic and replace internal
`_pb("GET", "/api/collections/...")` with the appropriate PBClient method
(`_pb().get_record(...)` or `_pb().list_page(...)`).

- [ ] **Step 4: Optionally pull descriptions from canonical prompts**

If FastMCP accepts `description=`:

```python
from app.agent.mcp_tools.prompts import TOOL_DESCRIPTIONS

@mcp.tool(description=TOOL_DESCRIPTIONS["pb_list_collections"])
def pb_list_collections() -> dict:
    return {"items": _pb().list_collections()}
```

If FastMCP requires the description in the docstring, copy from
`TOOL_DESCRIPTIONS["..."]` manually.

- [ ] **Step 5: Sanity**

```bash
python -c "import sys; sys.path.insert(0, '.'); import mcp_pb.server; print('OK')" 2>&1 | tail -3
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add mcp_pb/server.py
git commit -m "refactor(mcp_pb): use unified PBClient + canonical prompts

Replaced bespoke _http/_pb_auth/_pb with app.integrations.pb.PBClient.
10 PB tools each have a 1-2 line body. Tool descriptions sourced from
app.agent.mcp_tools.prompts."
```

---

## Task 9: Migrate `pb_tools.py` to AsyncPBClient

**Files:**
- Modify: `pb_tools.py`

- [ ] **Step 1: Find helper block**

```bash
grep -nE "^def _http|^def _pb_auth|^def _pb_sync|^async def _pb\b|^_pb_token\b" pb_tools.py
```

- [ ] **Step 2: Replace _http/_pb_auth/_pb_sync/_pb with AsyncPBClient**

Find the block from `# ---------- PB HTTP ----------` through the end of
`async def _pb(...)`. Replace with:

```python
# ---------- PB HTTP ----------
# Phase 1: replaced bespoke _http/_pb_auth/_pb_sync/_pb with the
# unified AsyncPBClient.

from app.integrations.pb import AsyncPBClient

_pb_client: AsyncPBClient | None = None


def _pb() -> AsyncPBClient:
    global _pb_client
    if _pb_client is None:
        _pb_client = AsyncPBClient(PB_URL, PB_EMAIL, PB_PASSWORD)
    return _pb_client
```

- [ ] **Step 3: Rewrite each @tool body**

Replace each `await _pb("GET", ...)` etc with the appropriate
`await _pb().<method>(...)`. Use the same mapping as Task 8.

**CRITICAL — auto-sync preservation:** After `pb_create` / `pb_update` /
`pb_delete`, KEEP the `_schedule_auto_sync(collection)` call. Example:

```python
@tool("pb_create", TOOL_DESCRIPTIONS["pb_create"], TOOL_ARG_SCHEMAS["pb_create"])
async def pb_create(args: dict) -> dict:
    collection = args["collection"]
    body = args["body"]
    try:
        result = await _pb().create_record(collection, body)
        _schedule_auto_sync(collection)
        return _ok(result)
    except PBError as e:
        return _err(str(e))
```

Import `PBError` from `app.integrations.pb` for catches.

- [ ] **Step 4: Tool descriptions from canonical prompts**

Replace inline `@tool(...)` description args:

Before:
```python
@tool("pb_search", "Search records ...", {...})
```

After:
```python
from app.agent.mcp_tools.prompts import TOOL_DESCRIPTIONS, TOOL_ARG_SCHEMAS

@tool("pb_search", TOOL_DESCRIPTIONS["pb_search"], TOOL_ARG_SCHEMAS["pb_search"])
```

- [ ] **Step 5: Sanity**

```bash
python -c "import pb_tools; print('OK')" 2>&1 | tail -3
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add pb_tools.py
git commit -m "refactor(pb_tools): use AsyncPBClient + canonical prompts

Replaced bespoke _http/_pb_auth/_pb_sync/_pb (~130 lines) with
app.integrations.pb.AsyncPBClient. _schedule_auto_sync(collection)
preserved on every write tool — that's business logic and stays here."
```

---

## Task 10: Migrate `server.py` — _pb_refresh_token + _pb_get_json

**Files:**
- Modify: `server.py`

- [ ] **Step 1: Add imports**

Near the existing `from app.settings import settings` line, add:

```python
from app.integrations.pb import (
    PBClient,
    PBError,
    refresh_token_into_env,
)
```

- [ ] **Step 2: Create module-level PBClient instance**

After the `POCKETBASE_URL = settings.pocketbase_url` block (around line 74),
add:

```python
# Unified PB client. Shared by:
#  - _pb_refresh_token (12h loop + 401 fallback) via refresh_token_into_env
#  - _pb_get_json (today-todos endpoint)
_pb_instance: PBClient | None = None


def _pb_client() -> PBClient:
    global _pb_instance
    if _pb_instance is None:
        _pb_instance = PBClient(
            POCKETBASE_URL,
            POCKETBASE_ADMIN_EMAIL,
            POCKETBASE_ADMIN_PASSWORD,
        )
    return _pb_instance
```

- [ ] **Step 3: Replace `_pb_refresh_token` body**

Find the existing definition (around line 80) and replace with:

```python
def _pb_refresh_token() -> bool:
    """Auth against PocketBase and mirror PB_TOKEN/PB_URL into os.environ.

    The os.environ mirror is the side-channel for child Bash subprocesses
    spawned by the Claude SDK — the CHAT-mode CHECKIN flow uses
    `$PB_TOKEN` and `$PB_URL` directly in curl commands.

    Returns True on success, False if creds are missing or auth failed.
    """
    if not (POCKETBASE_URL and POCKETBASE_ADMIN_EMAIL and POCKETBASE_ADMIN_PASSWORD):
        return False
    try:
        refresh_token_into_env(_pb_client())
        return True
    except PBError as e:
        log.warning("PB token refresh failed: %s", e)
        return False
```

Remove the old urllib boilerplate this replaces (~30 lines).

- [ ] **Step 4: Replace `_pb_get_json` body**

Find the existing definition (around line 1180) and replace with:

```python
def _pb_get_json(path: str) -> dict:
    """GET a PocketBase endpoint with auto-retry on 401. Raises _PBError
    on persistent failure.

    Phase 1 delegates to the unified PBClient, which already handles
    401-then-forced-reauth-then-one-retry.
    """
    if not POCKETBASE_URL:
        raise _PBError("PocketBase not configured")
    try:
        return _pb_client().request("GET", path, retry_on_401=True)
    except PBError as e:
        raise _PBError(str(e)) from e
```

- [ ] **Step 5: Verify the side-channel still works**

```bash
python -c "
import os
os.environ.pop('PB_TOKEN', None)
os.environ.pop('PB_URL', None)
import sys
sys.path.insert(0, '.')
from app.integrations.pb import PBClient, refresh_token_into_env
class _R:
    status = 200
    def read(self): import json; return json.dumps({'token': 'side-tok'}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): pass
pb = PBClient('http://localhost:8090', 'e', 'p')
pb._urlopen = lambda req, timeout=None: _R()
refresh_token_into_env(pb)
assert os.environ['PB_TOKEN'] == 'side-tok'
assert os.environ['PB_URL'] == 'http://localhost:8090'
print('OK side-channel verified')
"
```

Expected: `OK side-channel verified`.

- [ ] **Step 6: Parse check**

```bash
python -c "import ast; ast.parse(open('server.py', encoding='utf-8').read()); print('OK')"
```

Expected: `OK`.

- [ ] **Step 7: Verify no leftover os.environ.get for PB_TOKEN**

```bash
grep -n "os\.environ\.get(.PB_TOKEN" server.py
```

Expected: no output. The previous 2 reads inside `_pb_get_json` are gone.

- [ ] **Step 8: Commit**

```bash
git add server.py
git commit -m "refactor(server): _pb_refresh_token + _pb_get_json use PBClient

12h refresh loop + lifespan startup + the 401-after-refresh retry all
route through the unified PBClient. os.environ['PB_TOKEN'/'PB_URL']
side-channel for child Bash subprocesses preserved via
refresh_token_into_env.

The 2 documented os.environ.get('PB_TOKEN') reads from Phase 0 are
gone — PBClient holds the token in instance state. ~50 lines removed."
```

---

## Task 11: Verification + deploy + finish branch

- [ ] **Step 1: Run all unit tests**

```bash
python tests/test_io_utils.py
python tests/test_settings.py
python tests/test_pb_client.py
python -m pytest tests/notion_sync/
```

Expected: all green except pre-existing test_icons.

- [ ] **Step 2: Verify no PB_TOKEN os.environ reads in non-comment code**

```bash
grep -rE "os\.environ\.(get|setdefault).*PB_TOKEN" --include="*.py" . | grep -v __pycache__ | grep -v "^[^:]*:[^:]*#" | grep -v noqa
```

Expected: no matches.

- [ ] **Step 3: Deploy**

```powershell
deploy
```

Watch for: pip-install clean, health check passes, journal has no new ERROR.

- [ ] **Step 4: Staging smoke**

```powershell
$env:BASE='https://dashboard-server.tail4cfa2.ts.net'
$env:BRIDGE_COOKIE='bridge_session=...'
python tests/smoke_backend.py
```

Expected: `OK: all smoke checks passed`.

- [ ] **Step 5: Verify PB_TOKEN side-channel from inside the service**

```bash
ssh dashboard-server "sudo journalctl -u phone-bridge --since '5 minutes ago' --no-pager" | grep "PB token refreshed"
```

Expected: at least one `PB token refreshed (len=...)` line.

- [ ] **Step 6: Manual CHAT-mode CHECKIN test** (or fallback)

Open PWA, try a `\`\`\`checkin ... \`\`\`` message that exercises
$PB_TOKEN / $PB_URL. If round-trips OK, side-channel is intact.

Fallback if hard to test:

```bash
ssh dashboard-server "for pid in \$(pgrep -f 'phone-bridge'); do echo PID \$pid; sudo grep -aE 'PB_(TOKEN|URL)' /proc/\$pid/environ 2>/dev/null | tr '\0' '\n'; done | head -10"
```

Confirm PB_TOKEN + PB_URL are present in the running service env.

- [ ] **Step 7: Write Phase 1 completion report**

Append to CHANGELOG.md after Phase 0:

```markdown
## 2026-06-XX — Phase 1 · 统一 PB 客户端 + MCP 工具单源

**Branch:** `refactor/phase-1-pb-client`
**Commit range:** `<start>..<end>`
**Actual time:** <X> hours

### What landed
- `app/integrations/pb/{client,token,exceptions}.py` — unified PBClient,
  AsyncPBClient (to_thread), refresh_token_into_env side-channel
- 12 unit tests in `tests/test_pb_client.py`
- `notion_sync/pb_api.py` shrunk to 30-line shim (19 callers unchanged)
- `notion_sync/provisioner.py` SLF001 escapes removed
- `pocketbase/migrate_notion.py` PB section uses PBClient
- `mcp_pb/server.py` PB tools become 1-2 line PBClient calls (~130 lines removed)
- `pb_tools.py` 10 tools use AsyncPBClient; `_schedule_auto_sync` preserved
- `server.py` _pb_refresh_token + _pb_get_json delegate to PBClient
- `app/agent/mcp_tools/prompts.py` — canonical tool descriptions + arg schemas

### Gates
- ✅ test_pb_client 12/12, settings 4/4, io_utils 8/8, notion_sync 106/107
- ✅ smoke green on staging
- ✅ "PB token refreshed" log confirms side-channel
- ✅ no os.environ.get for PB_TOKEN remains in runtime code

### Deviations
- (any specifics)

### Next
👉 Phase 2 · 后端拆包 server.py → app/
New-window resume command: "继续重构路线图，从 Phase 2 开始"
```

- [ ] **Step 8: Update spec progress table**

Edit `docs/superpowers/specs/2026-06-06-refactor-roadmap.md`. Phase 1 row:

```
| 1 PB 统一 | ⏳ 待开始 | `refactor/phase-1-pb-client` | — | — | — |
```

→

```
| 1 PB 统一 | 🚧 已部署 待合并 | `refactor/phase-1-pb-client` | <today> | `<tip-sha>` | CHANGELOG §Phase 1 |
```

Update §下一步入口 to `Phase 2 · 后端拆包`.

- [ ] **Step 9: Commit docs**

```bash
git add CHANGELOG.md docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(changelog): Phase 1 completion report"
```

- [ ] **Step 10: Invoke finishing-a-development-branch**

Use `superpowers:finishing-a-development-branch`. Pick Option 1 (merge
locally). After merge, update spec from `🚧 已部署 待合并` to `✅ 已合并`
with merge SHA.

---

## Self-Review

**1. Spec coverage** — every Phase 1 spec checkbox:
- ✅ `PBClient` (sync) + `AsyncPBClient` (to_thread) → Task 2
- ✅ token-into-env helper → Task 3
- ✅ `prompts.py` single source → Task 7
- ✅ thin `pb_tools.py` decorator layer → Task 9
- ✅ thin `mcp_pb/server.py` → Task 8
- ✅ 5xx + 429 exponential backoff → Task 2
- ✅ `tests/test_pb_client.py` urlopen-mock → Task 2
- ✅ `todos_client.py` collapse: **DEFERRED to Phase 2**. Spec said "todos_client → ops"; Phase 1 keeps it as-is. Rationale: todos_client routes through Settings() per call already (Phase 0); migrating it to use PBClient is mechanical but expanding Phase 1's scope further is not worth it. Phase 2 picks it up.
- ✅ The spec also mentioned `ops.py` (business-level pure functions). Phase 1 simplifies: the PBClient class IS the API surface. No separate `ops.py` layer was added — that would have been an empty indirection with no consumers in Phase 1. Phase 2 can add `ops.py` if/when a business-level abstraction proves useful.

**2. Placeholder scan** — no TBD/TODO. `<today>` / `<tip-sha>` / `<X>` / `<start>..<end>` in Task 11 are runtime-filled and clearly marked.

**3. Type consistency**:
- `PBClient` / `AsyncPBClient` used identically Tasks 2-10
- Method names `list_page` / `list_all` / `get_record` etc consistent
- Exception names `PBError` / `PBHTTPError` / `PBAuthError` / `PBNetworkError` consistent
- `refresh_token_into_env(pb)` signature consistent
- `_pb()` helper name (Tasks 4, 8, 9) is module-scoped, same name in 3 separate files is intentional (no cross-import)
- `_pb_client` helper name (Task 10) chosen to avoid collision with the `_pb_client` global variable inside `pb_tools.py` / `mcp_pb/server.py` if those files are ever read in the same process (they aren't, but defensive naming)

**4. Order dependencies**:
- 1-3 (scaffolds + tests) → 4 (script) → 5 (shim) → 6 (provisioner)
  → 7 (prompts) → 8 (mcp_pb) → 9 (pb_tools) → 10 (server) → 11 (verify+merge)
- Tasks 8 & 9 both import from prompts (Task 7) — Task 7 must precede them.

**5. Honest scope**:
- Task 7's tool descriptions in this plan are samples. The executor MUST replace them with the verbatim pb_tools.py strings.
- Task 10's PB_TOKEN side-channel verification (Step 6) has a `/proc/<pid>/environ` fallback for when constructing a working CHECKIN test is impractical.
- Task 5's shim approach (vs rewriting 19 callers) is deliberate; Phase 2's decomposition removes the shim.

---

**Plan complete.**
