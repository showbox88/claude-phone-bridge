"""Backend smoke test for phone-bridge.

Runs against a live server URL. Exercises read-only endpoints + a WS roundtrip.
No external deps beyond stdlib + websockets (already pulled by uvicorn[standard]).

Usage:
    # Localhost dev server:
    BRIDGE_COOKIE='session=...' python tests/smoke_backend.py

    # Production:
    BASE=https://dashboard-server.tail4cfa2.ts.net \\
      BRIDGE_COOKIE='session=...' \\
      python tests/smoke_backend.py

Exits 0 on success, non-zero on first failure with a clear marker line.
"""
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

import websockets

BASE = os.environ.get("BASE", "http://127.0.0.1:8001").rstrip("/")
COOKIE = os.environ.get("BRIDGE_COOKIE", "")
WS_BASE = BASE.replace("http://", "ws://").replace("https://", "wss://")


def _step(label):
    sys.stdout.write(f"  - {label} ... ")
    sys.stdout.flush()


def _ok(detail=""):
    sys.stdout.write(f"OK {detail}\n")


def _fail(detail):
    sys.stdout.write(f"FAIL\n    {detail}\n")
    sys.exit(1)


def _http(method, path, body=None, expect=200):
    url = BASE + path
    headers = {"Cookie": COOKIE} if COOKIE else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
            payload = r.read()
    except urllib.error.HTTPError as e:
        code = e.code
        payload = e.read()
    if code != expect:
        _fail(f"{method} {path}: expected {expect}, got {code}: {payload[:200]!r}")
    try:
        return json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        return {"_raw": payload[:200].decode("utf-8", errors="replace")}


async def _ws_roundtrip():
    """Connect WS, expect a 'hello' frame back."""
    ws_url = f"{WS_BASE}/ws"
    headers = [("Cookie", COOKIE)] if COOKIE else []
    async with websockets.connect(ws_url, additional_headers=headers, open_timeout=10) as ws:
        first = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = json.loads(first)
        if msg.get("type") != "hello":
            raise RuntimeError(f"first WS frame not 'hello', got {msg.get('type')!r}")
        return msg


async def main():
    print(f"Smoke target: {BASE}")
    print(f"Cookie: {'set' if COOKIE else 'NOT SET (some checks will fail)'}")
    print()

    _step("GET /api/health")
    h = _http("GET", "/api/health")
    if not h.get("ok"):
        _fail(f"health.ok != True: {h}")
    if COOKIE and "mode" not in h:
        _fail(f"authed /api/health missing mode (cookie expired?): {h}")
    _ok(f"(mode={h.get('mode', '?')}, model={h.get('model', '?') or 'default'})")

    _step("GET /api/meta")
    m = _http("GET", "/api/meta")
    modes = m.get("modes")
    models = m.get("models")
    if not isinstance(modes, list) or not modes:
        _fail(f"meta.modes not a non-empty list: {m}")
    if not isinstance(models, list) or not models:
        _fail(f"meta.models not a non-empty list: {m}")
    _ok(f"({len(modes)} modes, {len(models)} models)")

    _step("GET /api/sessions")
    sess = _http("GET", "/api/sessions")
    items = sess.get("sessions") if isinstance(sess, dict) else None
    if not isinstance(items, list):
        _fail(f"sessions payload missing 'sessions' list: {sess}")
    _ok(f"(current={str(sess.get('current') or '')[:8]}..., {len(items)} sessions)")

    _step("GET /api/today-todos")
    td = _http("GET", "/api/today-todos")
    _ok(f"({len(td.get('items', []))} todos)")

    _step("WS /ws hello frame")
    hello = await _ws_roundtrip()
    _ok(f"(session={str(hello.get('session_id', '?'))[:8]}...)")

    print()
    print("OK: all smoke checks passed")


if __name__ == "__main__":
    t0 = time.time()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        _fail(f"uncaught: {type(exc).__name__}: {exc}")
    print(f"   {time.time() - t0:.1f}s")
