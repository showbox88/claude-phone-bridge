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
