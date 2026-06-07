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
        retries=3, retry_initial_backoff=0.0, retry_jitter_max=0.0,
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
        assert e.attempts == 3
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
