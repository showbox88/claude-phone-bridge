# Hidden Auth via Super Link — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide Phone Bridge's auth surface from the public internet — replace the public login page with a secret high-entropy "super link" that gates a password+TOTP login, and return a misleading `503` decoy for every other unauthenticated request.

**Architecture:** A single FastAPI HTTP middleware decides every request's fate: valid device cookie → real app; first path segment matches the stored super-link hash → password+TOTP gate (enrolls the device on success); anything else → a generic nginx-style `503` decoy. The super link is stored hashed (sha256), compared in constant time, never logged, and rotatable via an SSH-runnable CLI. Existing trusted devices keep working via the unchanged sliding session cookie.

**Tech Stack:** Python 3.11, FastAPI/Starlette, pytest + Starlette `TestClient` (httpx), bcrypt/pyotp (existing auth).

**Spec:** [docs/superpowers/specs/2026-06-10-hidden-auth-superlink-design.md](../specs/2026-06-10-hidden-auth-superlink-design.md)

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `app/settings.py` | `bridge_cookie_days` default | Modify (30 → 90) |
| `auth.py` | `AuthState` — add `super_link_hash` field + set/verify helpers | Modify |
| `app/auth/views.py` | Shared HTML helpers (`_page`, CSS, `_ua_short`, `_html_escape`) extracted from `pages.py` so both `pages.py` and `gate.py` can use them without a circular import | Create |
| `app/auth/gate.py` | The super-link auth gate: render login form (GET) + verify & enroll (POST) | Create |
| `app/auth/middleware.py` | Rewrite the 3-outcome routing logic + `decoy_response()` | Modify |
| `app/auth/pages.py` | Import shared helpers from `views.py`; remove public `/login`, `/setup`, `/setup/verify`; keep `/devices`, `/devices/revoke`, `/logout` (cookie-gated) | Modify |
| `app/auth/cli.py` | SSH-runnable CLI: `rotate-link`, `init`, `list-devices`, `revoke` | Create |
| `static/index.html` | Add `crossorigin="use-credentials"` to the manifest link so the cookie-gated manifest loads on trusted devices | Modify |
| `tests/test_superlink_auth.py` | Unit + TestClient tests for the new behavior | Create |
| `tests/smoke_backend.py` | Add "no-cookie → 503 decoy" check | Modify |
| `CLAUDE.md`, `docs/operations/superlink-runbook.md` | Document the new model + runbook | Modify/Create |

---

## Task 1: Bump session cookie default to 90 days

**Files:**
- Modify: `app/settings.py:50`
- Test: `tests/test_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings.py`:

```python
def test_cookie_days_default_is_90():
    from app.settings import Settings
    s = Settings(_env_file=None)
    assert s.bridge_cookie_days == 90
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings.py::test_cookie_days_default_is_90 -v`
Expected: FAIL (`assert 30 == 90`)

- [ ] **Step 3: Change the default**

In `app/settings.py`, line 50, change:

```python
    bridge_cookie_days: int = 30
```

to:

```python
    bridge_cookie_days: int = 90
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings.py::test_cookie_days_default_is_90 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/settings.py tests/test_settings.py
git commit -m "feat(auth): raise session cookie default 30->90 days"
```

---

## Task 2: Add super-link storage + verify helpers to AuthState

**Files:**
- Modify: `auth.py` (imports, `_load` default, new methods)
- Test: `tests/test_superlink_auth.py` (Create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_superlink_auth.py`:

```python
"""Tests for the hidden super-link auth model."""
from pathlib import Path

import auth as auth_mod


def _fresh_state(tmp_path: Path) -> auth_mod.AuthState:
    st = auth_mod.AuthState(tmp_path / ".bridge_auth.json")
    st.initialize("correct horse battery staple")  # sets password + totp
    return st


def test_super_link_absent_by_default(tmp_path):
    st = _fresh_state(tmp_path)
    assert st.has_super_link() is False
    assert st.verify_super_link("anything") is False


def test_set_super_link_returns_plaintext_and_verifies(tmp_path):
    st = _fresh_state(tmp_path)
    token = st.set_super_link()
    assert isinstance(token, str) and len(token) >= 40
    assert st.has_super_link() is True
    assert st.verify_super_link(token) is True
    assert st.verify_super_link(token + "x") is False
    assert st.verify_super_link("") is False


def test_rotate_invalidates_old_link(tmp_path):
    st = _fresh_state(tmp_path)
    old = st.set_super_link()
    new = st.set_super_link()
    assert old != new
    assert st.verify_super_link(old) is False
    assert st.verify_super_link(new) is True


def test_super_link_persisted_as_hash_not_plaintext(tmp_path):
    st = _fresh_state(tmp_path)
    token = st.set_super_link()
    raw = (tmp_path / ".bridge_auth.json").read_text(encoding="utf-8")
    assert token not in raw          # plaintext never on disk
    assert "super_link_hash" in raw  # only the hash
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_superlink_auth.py -v`
Expected: FAIL (`AttributeError: 'AuthState' object has no attribute 'has_super_link'`)

- [ ] **Step 3: Implement the helpers**

In `auth.py`, add `import hmac` to the import block (near `import hashlib`).

In `AuthState._load`, update BOTH default dicts (the `if not exists` branch and the `except` branch) to include the new key:

```python
            return {"password_hash": None, "totp_secret": None, "devices": {},
                    "super_link_hash": None}
```

Add these methods to `AuthState` (after `revoke`/`list_devices`, before the rate-limit section):

```python
    # ---- super link (hidden auth gate) ----------------------------------
    def has_super_link(self) -> bool:
        return bool(self.data.get("super_link_hash"))

    def set_super_link(self) -> str:
        """Mint a fresh super-link secret, store only its hash, return plaintext.

        Rotating (calling again) invalidates the previous link immediately.
        """
        secret = secrets.token_urlsafe(36)  # ~48 url-safe chars
        with self.lock:
            self.data["super_link_hash"] = _hash_token(secret)
            self._save_locked()
        return secret

    def verify_super_link(self, candidate: str) -> bool:
        stored = self.data.get("super_link_hash")
        if not stored or not candidate:
            return False
        return hmac.compare_digest(stored, _hash_token(candidate))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_superlink_auth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add auth.py tests/test_superlink_auth.py
git commit -m "feat(auth): add hashed super-link storage + constant-time verify"
```

---

## Task 3: Extract shared HTML view helpers into `app/auth/views.py`

This breaks the future circular import (`middleware` → `gate` → view helpers, while `pages` also needs them). Pure move, no behavior change.

**Files:**
- Create: `app/auth/views.py`
- Modify: `app/auth/pages.py` (delete the moved defs, import them instead)
- Test: `tests/test_superlink_auth.py` (add import-smoke test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_superlink_auth.py`:

```python
def test_view_helpers_importable_from_views():
    from app.auth.views import _page, _AUTH_PAGE_CSS, _ua_short, _html_escape
    html = _page("T", "<p>x</p>")
    assert html.status_code == 200
    assert _html_escape("<a>") == "&lt;a&gt;"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_superlink_auth.py::test_view_helpers_importable_from_views -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.auth.views'`)

- [ ] **Step 3: Create `app/auth/views.py`**

Move the four helpers verbatim out of `pages.py` into a new file:

```python
"""Shared HTML rendering helpers for the auth surface (login gate + device
management). Extracted from pages.py so both pages.py and gate.py can use them
without a circular import via middleware."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse

_AUTH_PAGE_CSS = """
<PASTE THE EXACT CONTENTS of pages.py's _AUTH_PAGE_CSS string here>
"""


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Phone Bridge</title>
<style>{_AUTH_PAGE_CSS}</style></head>
<body><div class="wrap"><div class="card">{body}</div></div></body></html>"""
    return HTMLResponse(html, status_code=status)


def _ua_short(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    if "iPhone" in ua: return "iPhone"
    if "iPad" in ua: return "iPad"
    if "Android" in ua: return "Android"
    if "Macintosh" in ua: return "Mac"
    if "Windows" in ua: return "Windows"
    if "Linux" in ua: return "Linux"
    return "device"


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
              .replace('"', "&quot;").replace("'", "&#39;"))
```

> NOTE: Copy the `_AUTH_PAGE_CSS` value exactly from `app/auth/pages.py` (lines 24–60 in the current file). Do not retype it.

- [ ] **Step 4: Update `pages.py` to import from views**

In `app/auth/pages.py`: delete the `_AUTH_PAGE_CSS`, `_page`, `_ua_short`, `_html_escape` definitions, and add to the import block:

```python
from app.auth.views import _AUTH_PAGE_CSS, _page, _ua_short, _html_escape  # noqa: F401
```

(Keep `_AUTH_PAGE_CSS` in the import list even if unused directly — harmless and explicit.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_superlink_auth.py -v && python -c "import app.auth.pages"`
Expected: PASS + clean import (no circular-import error)

- [ ] **Step 6: Commit**

```bash
git add app/auth/views.py app/auth/pages.py tests/test_superlink_auth.py
git commit -m "refactor(auth): extract shared HTML view helpers into views.py"
```

---

## Task 4: Build the super-link auth gate (`app/auth/gate.py`)

The gate renders the password+TOTP form on GET and enrolls the device on POST. The form POSTs back to the same secret path (so nothing telltale is exposed). Reuses the existing rate limiter.

**Files:**
- Create: `app/auth/gate.py`
- Test: `tests/test_superlink_auth.py` (TestClient cases come in Task 6 after middleware wiring; here test the POST verifier logic directly)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_superlink_auth.py`:

```python
import asyncio
from starlette.requests import Request


def _post_request(path, form_bytes, ip="1.2.3.4"):
    """Minimal ASGI scope for a POST with urlencoded form body."""
    scope = {
        "type": "http", "method": "POST", "path": path, "raw_path": path.encode(),
        "headers": [(b"content-type", b"application/x-www-form-urlencoded"),
                    (b"user-agent", b"pytest")],
        "query_string": b"", "client": (ip, 0), "scheme": "https", "server": ("h", 443),
    }
    sent = {"done": False}
    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": form_bytes, "more_body": False}
    return Request(scope, receive)


def test_gate_post_rejects_bad_credentials(tmp_path, monkeypatch):
    # Driven via asyncio.run() (sync test) so it runs without pytest-asyncio,
    # which is NOT installed in the local dev environment.
    import app.auth.gate as gate
    st = _fresh_state(tmp_path)
    monkeypatch.setattr(gate, "auth_state", st)
    req = _post_request("/" + st.set_super_link(),
                        b"password=wrong&code=000000&device_name=x")
    resp = asyncio.run(gate.superlink_gate(req))
    assert resp.status_code == 401
    assert not st.list_devices()  # no device enrolled on failure
```

> The valid-credential path needs a real TOTP code; it is covered end-to-end by the TestClient test in Task 6. This test pins the rejection path.
>
> **Interpreter:** run all tests in this plan with the project's `python` (the global interpreter on this Windows box already has fastapi/pyotp/bcrypt/httpx). All new tests are synchronous (TestClient is sync; the gate coroutine is driven via `asyncio.run`), so `pytest-asyncio` is NOT required.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_superlink_auth.py::test_gate_post_rejects_bad_credentials -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.auth.gate'`)

- [ ] **Step 3: Implement the gate**

Create `app/auth/gate.py`:

```python
"""The hidden super-link auth gate.

Reached ONLY when the request's first path segment matches the stored
super-link hash (the middleware dispatches here). GET renders the password +
TOTP form; POST verifies and, on success, enrols the current device (issues a
session cookie) and redirects into the app. The form posts back to the same
secret path, so no telltale auth path is ever exposed.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

import auth as auth_mod

from app.auth.state import _COOKIE_SECONDS, auth_state
from app.auth.views import _page, _ua_short


def _form(path: str, *, error: str = "") -> HTMLResponse:
    err = f'<p class="error">{error}</p>' if error else ""
    return _page("Sign in", f"""
<h1>Sign in</h1>
<p class="sub">Enter password and the 6-digit code from your authenticator.</p>
<form method="post" action="{path}">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" required autofocus autocomplete="current-password">
  <label for="code">6-digit code</label>
  <input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required autocomplete="one-time-code">
  <label for="device_name">Name this device (optional)</label>
  <input id="device_name" name="device_name" type="text" maxlength="40">
  <button type="submit">Sign in</button>
</form>{err}""", status=200 if not error else 401)


async def superlink_gate(request: Request):
    """Dispatched by the middleware for a path matching the super-link hash."""
    path = request.url.path
    if request.method == "GET":
        return _form(path)

    # POST — verify credentials
    ip = auth_mod.client_ip(request)
    allowed, retry_after = auth_state.can_attempt(ip)
    if not allowed:
        return _form(path, error=f"Too many attempts. Try again in {retry_after}s.")
    form = await request.form()
    password = str(form.get("password", ""))
    code = str(form.get("code", ""))
    device_name = str(form.get("device_name", ""))
    if not (auth_state.verify_password(password) and auth_state.verify_totp(code)):
        auth_state.record_fail(ip)
        return _form(path, error="Invalid password or code.")
    auth_state.clear_fails(ip)
    name = (device_name.strip() or _ua_short(request))[:40]
    token = auth_state.issue_device_token(
        name=name, ip=ip, ua=request.headers.get("user-agent", ""),
    )
    resp = RedirectResponse("/", status_code=303)
    auth_mod.set_session_cookie(resp, token, max_age=_COOKIE_SECONDS)
    return resp
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_superlink_auth.py::test_gate_post_rejects_bad_credentials -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/auth/gate.py tests/test_superlink_auth.py
git commit -m "feat(auth): add super-link password+TOTP gate"
```

---

## Task 5: Rewrite the middleware — 3 outcomes + decoy

**Files:**
- Modify: `app/auth/middleware.py` (full rewrite of `auth_middleware`, new `decoy_response`, trimmed public lists)
- Test: covered by Task 6 TestClient suite

- [ ] **Step 1: Implement the rewrite**

Replace the body of `app/auth/middleware.py` from the `_PUBLIC_*` definitions through `auth_middleware` with:

```python
from fastapi import Request
from fastapi.responses import Response

import auth as auth_mod

from app.auth.gate import superlink_gate
from app.auth.state import _COOKIE_SECONDS, auth_state

# Only these stay reachable WITHOUT a device cookie:
#  - /api/health: the deploy tool health-checks it over the public Funnel URL.
#  - the two RFC well-known OAuth paths: claude.ai's connector probes them
#    unauthenticated during OAuth discovery for the sibling mcp_pb service.
_PUBLIC_EXACT = {
    "/api/health",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server/mcp",
}

# Generic nginx-style 503 — misdirects scanners toward "backend is down",
# revealing nothing about Phone Bridge or that an auth system exists here.
_DECOY_BODY = (
    b"<html>\r\n<head><title>503 Service Temporarily Unavailable</title></head>\r\n"
    b"<body>\r\n<center><h1>503 Service Temporarily Unavailable</h1></center>\r\n"
    b"<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)


def decoy_response() -> Response:
    return Response(content=_DECOY_BODY, status_code=503,
                    media_type="text/html", headers={"Retry-After": "120"})


def _current_device(request: Request) -> dict | None:
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if not token:
        return None
    return auth_state.lookup_token(
        token,
        ip=auth_mod.client_ip(request),
        ua=request.headers.get("user-agent", ""),
    )


async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # 1. Always-public infra (health + OAuth discovery).
    if path in _PUBLIC_EXACT:
        return await call_next(request)

    # 2. The hidden gate: first path segment matches the super-link secret.
    #    The secret IS the path — there is no telltale auth path.
    seg = path.lstrip("/").split("/", 1)[0]
    if seg and auth_state.verify_super_link(seg):
        return await superlink_gate(request)

    # 3. Trusted device → the real app, with sliding-cookie refresh.
    device = _current_device(request)
    if device is not None:
        request.state.device = device
        response = await call_next(request)
        token = request.cookies.get(auth_mod.COOKIE_NAME)
        if token:
            auth_mod.set_session_cookie(response, token, max_age=_COOKIE_SECONDS)
        return response

    # 4. Everything else → misleading decoy. No redirect, no login form, no hint.
    return decoy_response()
```

Delete the now-unused `_PUBLIC_PREFIXES`, `_is_public`, and `_wants_html` (the new logic doesn't branch on Accept headers). Keep the module docstring but update it to describe the 3-outcome model.

> IMPORTANT: `_current_device` is imported by `app/auth/pages.py` and `app/api/meta.py`. Keep its name and signature unchanged (it stays in this module).

- [ ] **Step 2: Verify import graph is clean**

Run: `python -c "import app.main"`
Expected: no `ImportError` / no circular-import error.

- [ ] **Step 3: Commit**

```bash
git add app/auth/middleware.py
git commit -m "feat(auth): middleware serves decoy 503; auth only via super link"
```

---

## Task 6: Remove public login/setup routes + TestClient integration tests

**Files:**
- Modify: `app/auth/pages.py` (remove `/login`, `/setup`, `/setup/verify`; keep `/devices`, `/devices/revoke`, `/logout`)
- Test: `tests/test_superlink_auth.py` (TestClient suite)

- [ ] **Step 1: Write the failing TestClient suite**

Append to `tests/test_superlink_auth.py`:

```python
import pyotp
from fastapi.testclient import TestClient


def _app_with_state(tmp_path, monkeypatch):
    """Build the real app but point every auth_state reference at a tmp file."""
    st = _fresh_state(tmp_path)
    import app.auth.state as state_mod
    import app.auth.middleware as mw
    import app.auth.gate as gate
    import app.auth.pages as pages
    monkeypatch.setattr(state_mod, "auth_state", st, raising=False)
    monkeypatch.setattr(mw, "auth_state", st, raising=False)
    monkeypatch.setattr(gate, "auth_state", st, raising=False)
    monkeypatch.setattr(pages, "auth_state", st, raising=False)
    import app.main as main
    return main.app, st


def test_no_cookie_root_returns_decoy_503(tmp_path, monkeypatch):
    app, st = _app_with_state(tmp_path, monkeypatch)
    client = TestClient(app, follow_redirects=False)
    r = client.get("/")
    assert r.status_code == 503
    assert "nginx" in r.text
    assert "Phone Bridge" not in r.text   # no identity leak


def test_old_login_path_is_decoy(tmp_path, monkeypatch):
    app, st = _app_with_state(tmp_path, monkeypatch)
    client = TestClient(app, follow_redirects=False)
    assert client.get("/login").status_code == 503
    assert client.get("/setup").status_code == 503


def test_super_link_get_renders_gate(tmp_path, monkeypatch):
    app, st = _app_with_state(tmp_path, monkeypatch)
    secret = st.set_super_link()
    client = TestClient(app, follow_redirects=False)
    r = client.get(f"/{secret}")
    assert r.status_code == 200
    assert "Sign in" in r.text


def test_super_link_post_enrolls_device(tmp_path, monkeypatch):
    app, st = _app_with_state(tmp_path, monkeypatch)
    secret = st.set_super_link()
    code = pyotp.TOTP(st.totp_secret()).now()
    client = TestClient(app, follow_redirects=False)
    r = client.post(f"/{secret}", data={
        "password": "correct horse battery staple",
        "code": code, "device_name": "Test"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert auth_mod.COOKIE_NAME in r.headers.get("set-cookie", "")
    assert len(st.list_devices()) == 1


def test_health_stays_public(tmp_path, monkeypatch):
    app, st = _app_with_state(tmp_path, monkeypatch)
    client = TestClient(app, follow_redirects=False)
    assert client.get("/api/health").status_code == 200
```

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/test_superlink_auth.py -v`
Expected: the new TestClient tests FAIL — `/login`/`/setup` still return redirects (not 503) because the routes are still registered.

- [ ] **Step 3: Remove the public auth routes from `pages.py`**

In `app/auth/pages.py`, delete these route handlers entirely:
- `@pages_router.get("/setup")` → `setup_get`
- `@pages_router.post("/setup")` → `setup_post`
- `@pages_router.get("/setup/verify")` → `setup_verify_get`
- `@pages_router.post("/setup/verify")` → `setup_verify_post`
- `@pages_router.get("/login")` → `login_get`
- `@pages_router.post("/login")` → `login_post`

KEEP: `/logout` (GET+POST), `/devices`, `/devices/revoke`. These are reached only with a valid cookie (middleware outcome 3), so they need no public exposure. Remove now-unused imports (`Form` is still used by `/devices/revoke`; `socket`, `auth_mod.otpauth_uri`, `qr_svg` become unused — delete the unused ones to keep the file clean).

> First-time setup (password + TOTP) no longer has a web route — it now happens via the CLI in Task 7 (`init`). The current production install is already initialized, so this only matters for a fresh deploy.

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `python -m pytest tests/test_superlink_auth.py -v`
Expected: PASS (all cases)

- [ ] **Step 5: Commit**

```bash
git add app/auth/pages.py tests/test_superlink_auth.py
git commit -m "feat(auth): remove public login/setup routes; gate is super-link only"
```

---

## Task 7: SSH management CLI (`app/auth/cli.py`)

**Files:**
- Create: `app/auth/cli.py`
- Test: `tests/test_superlink_auth.py` (CLI uses the same AuthState API already tested; add one wiring test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_superlink_auth.py`:

```python
def test_cli_rotate_link_sets_verifiable_secret(tmp_path, monkeypatch, capsys):
    st = _fresh_state(tmp_path)
    import app.auth.cli as cli
    monkeypatch.setattr(cli, "auth_state", st, raising=False)
    cli.main(["rotate-link"])
    out = capsys.readouterr().out.strip()
    # the printed secret (last whitespace-delimited token containing '/') verifies
    printed = [w for line in out.splitlines() for w in line.split() if "/" in w]
    secret = printed[-1].rsplit("/", 1)[-1]
    assert st.verify_super_link(secret) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_superlink_auth.py::test_cli_rotate_link_sets_verifiable_secret -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.auth.cli'`)

- [ ] **Step 3: Implement the CLI**

Create `app/auth/cli.py`:

```python
"""Host-side auth management CLI (run over SSH).

    python -m app.auth.cli rotate-link    # mint a new super link (prints it ONCE)
    python -m app.auth.cli init           # first-time password+TOTP setup
    python -m app.auth.cli list-devices
    python -m app.auth.cli revoke <hash>

The super link is the only public door to log in; `rotate-link` is also the
recovery path if the link is lost or every trusted device dies (reachable in
China via the user's own VPN -> SSH).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

import auth as auth_mod  # noqa: F401  (kept for parity / future use)

from app.auth.state import auth_state

_BASE = os.environ.get("BRIDGE_PUBLIC_URL", "https://dashboard-server.tail4cfa2.ts.net").rstrip("/")


def _rotate_link() -> int:
    secret = auth_state.set_super_link()
    print("New super link (save it now — it will NOT be shown again):")
    print(f"  {_BASE}/{secret}")
    print("The previous link (if any) is now invalid.")
    return 0


def _init() -> int:
    if auth_state.is_initialized():
        print("Already initialized. Use rotate-link to mint a super link.", file=sys.stderr)
        return 1
    pw = getpass.getpass("Master password (min 12 chars): ")
    secret = auth_state.initialize(pw)
    print(f"TOTP secret (add to your authenticator): {secret}")
    print("Now run: python -m app.auth.cli rotate-link")
    return 0


def _list_devices() -> int:
    for d in auth_state.list_devices():
        print(f"  {d['hash'][:12]}  {d.get('name','?'):20}  last_ip={d.get('last_ip','')}")
    return 0


def _revoke(token_hash: str) -> int:
    ok = auth_state.revoke(token_hash)
    print("revoked" if ok else "no such device")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.auth.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rotate-link")
    sub.add_parser("init")
    sub.add_parser("list-devices")
    rv = sub.add_parser("revoke")
    rv.add_argument("hash")
    args = p.parse_args(argv)
    if args.cmd == "rotate-link":
        return _rotate_link()
    if args.cmd == "init":
        return _init()
    if args.cmd == "list-devices":
        return _list_devices()
    if args.cmd == "revoke":
        return _revoke(args.hash)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_superlink_auth.py::test_cli_rotate_link_sets_verifiable_secret -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/auth/cli.py tests/test_superlink_auth.py
git commit -m "feat(auth): add SSH management CLI (rotate-link/init/list/revoke)"
```

---

## Task 8: Cookie-gate PWA assets (manifest stealth)

The cookie-gated manifest must still load on trusted devices. `<link rel="manifest">` does NOT send cookies by default, so add `crossorigin="use-credentials"`.

**Files:**
- Modify: `static/index.html:11`
- Test: manual (verified in Task 9)

- [ ] **Step 1: Edit the manifest link**

In `static/index.html`, line 11, change:

```html
  <link rel="manifest" href="/manifest.json">
```

to:

```html
  <link rel="manifest" href="/manifest.json" crossorigin="use-credentials">
```

- [ ] **Step 2: Sanity check the static-asset test still passes**

Run: `python -m pytest tests/test_static_assets.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat(auth): send credentials for manifest so cookie-gated PWA loads"
```

> Verification that the PWA still installs/loads on a trusted device happens in Task 9. **Fallback if it breaks:** if a trusted device cannot load `/manifest.json` or `/icon.svg` after gating, add `"/icon.svg"` (a generic icon, negligible tell) back to `_PUBLIC_EXACT`; do NOT re-expose `/manifest.json` (it leaks the app name).

---

## Task 9: Update smoke test + docs, then full verification

**Files:**
- Modify: `tests/smoke_backend.py`
- Modify: `CLAUDE.md`
- Create: `docs/operations/superlink-runbook.md`

- [ ] **Step 1: Add a no-cookie decoy check to the smoke test**

In `tests/smoke_backend.py`, inside `main()` before the final success print, add:

```python
    _step("GET / without cookie -> decoy 503")
    req = urllib.request.Request(BASE + "/", method="GET")  # no Cookie header
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    if code != 503:
        _fail(f"unauthed / expected 503 decoy, got {code}")
    _ok()
```

- [ ] **Step 2: Run the unit suite green**

Run: `python -m pytest tests/test_superlink_auth.py tests/test_settings.py tests/test_static_assets.py -v`
Expected: PASS (all)

- [ ] **Step 3: Update `CLAUDE.md`**

Replace the stale "Authentication is implicit: only devices logged into the user's Tailscale account can reach the URL." paragraph (near the top) with:

```markdown
Authentication: the public surface returns a generic `503` decoy to everything
unauthenticated. The only login door is a secret **super link** (a
high-entropy URL) that gates a password + TOTP form; passing it enrols the
device (90-day sliding cookie). Manage/rotate via SSH:
`python -m app.auth.cli rotate-link`. See
[docs/operations/superlink-runbook.md](docs/operations/superlink-runbook.md).
```

- [ ] **Step 4: Write the runbook**

Create `docs/operations/superlink-runbook.md` documenting: what the super link is, "save it in your password manager", how to rotate it over SSH, the decoy behavior, the device list/revoke commands, and the recovery path (SSH via VPN from China). (Prose — no code placeholders; describe the `app.auth.cli` commands from Task 7.)

- [ ] **Step 5: Commit**

```bash
git add tests/smoke_backend.py CLAUDE.md docs/operations/superlink-runbook.md
git commit -m "docs(auth): document super-link model + smoke-test the decoy"
```

- [ ] **Step 6: Deploy to staging + first lockdown (manual, NOT from an active chat)**

1. Confirm you are on branch `feature/hidden-auth-superlink`.
2. Deploy to the VM (`deploy`). The current install is already initialized, so existing devices keep their cookies.
3. SSH: `cd /home/dev/phone-bridge && set -a; . ./.env; set +a && .venv/bin/python -m app.auth.cli rotate-link` → save the printed super link.
4. Verify decoy: from a browser with NO cookie, `GET /` → generic 503; `/login` → 503.
5. Verify gate: open the super link → password+TOTP form → log in → lands in the app; `.venv/bin/python -m app.auth.cli list-devices` shows the new device.
6. Verify PWA on a trusted device: app loads, installs, service worker active (Task 8 fallback if not).
7. Run smoke: `BASE=https://dashboard-server.tail4cfa2.ts.net BRIDGE_COOKIE='bridge_session=...' python tests/smoke_backend.py` → `OK: all smoke checks passed`.

- [ ] **Step 7: Merge**

Only after staging soak is clean (per refactor roadmap): merge `feature/hidden-auth-superlink` → `main`, deploy, re-run smoke.

---

## Self-Review

- **Spec coverage:** §2 goal (decoy + super link + password/TOTP + trusted devices + SSH backstop) → Tasks 2,4,5,6,7. §4.1 three outcomes → Task 5. §4.2 super link (hashed/constant-time/rotatable) → Task 2. §4.3 enrollment lock (remove public login) → Task 6. §4.4 decoy 503+Retry-After → Task 5. §4.5 CLI → Task 7. §7 cookie 90 days → Task 1; manifest stealth → Task 8; health stays public → Task 5 (`_PUBLIC_EXACT`). §8 testing → Tasks 6,9. §9 rollout/branch → Task 9. Recovery codes / mTLS explicitly out of scope — no task, correct.
- **Placeholder scan:** none. The only "paste exact contents" directive (Task 3 CSS) is a deliberate verbatim move with the source lines cited.
- **Type/name consistency:** `set_super_link()`, `verify_super_link()`, `has_super_link()`, `decoy_response()`, `superlink_gate()`, `_current_device()`, `_PUBLIC_EXACT`, `auth_mod.COOKIE_NAME`, `_COOKIE_SECONDS` used consistently across Tasks 2/4/5/6/7.
