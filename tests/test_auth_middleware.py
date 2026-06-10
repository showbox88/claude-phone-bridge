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
