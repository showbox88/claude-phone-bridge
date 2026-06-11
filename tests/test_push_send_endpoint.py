"""POST /api/push/send: loopback-only push trigger for PB hook."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.push import router


def _client():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_send_calls_send_to_all_when_loopback():
    client = _client()
    with patch("app.api.push.push") as p, patch(
        "app.api.push._is_loopback", return_value=True
    ):
        r = client.post(
            "/api/push/send",
            json={"title": "T", "body": "B", "tag": "x"},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    p.send_to_all.assert_called_once_with("T", "B", "x")


def test_send_rejects_non_loopback_client():
    """When the request appears to come from a non-loopback peer, return 403."""
    client = _client()
    # TestClient's default client.host is "testclient" — not 127.0.0.1 — so
    # the endpoint should refuse without any header trickery.
    with patch("app.api.push.push") as p:
        r = client.post(
            "/api/push/send",
            json={"title": "T", "body": "B"},
        )
    assert r.status_code == 403
    p.send_to_all.assert_not_called()


def test_send_accepts_missing_tag():
    client = _client()
    with patch("app.api.push.push") as p, patch(
        "app.api.push._is_loopback", return_value=True
    ):
        r = client.post(
            "/api/push/send",
            json={"title": "T", "body": "B"},
        )
    assert r.status_code == 200
    p.send_to_all.assert_called_once_with("T", "B", None)


def test_send_accepts_empty_payload_silently():
    """A misconfigured PB hook sending an empty body still 200s — title/body
    coerce to '' and tag to None. This is intentional: the loopback-only
    constraint limits the blast radius enough that we prefer silent
    acceptance over hard 422 (which would mask the hook bug, not fix it)."""
    client = _client()
    with patch("app.api.push.push") as p, patch(
        "app.api.push._is_loopback", return_value=True
    ):
        r = client.post("/api/push/send", json={})
    assert r.status_code == 200
    p.send_to_all.assert_called_once_with("", "", None)
