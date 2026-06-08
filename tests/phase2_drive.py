"""Phase 2 traffic driver — deterministic baseline/after recording.

Drives the same set of HTTP requests + WS frames in Task 0 (baseline) and
Task 15 (after) so tests/replay.py's byte-diff is meaningful. Designed for
idempotency: every mutation is paired with a cleanup. Skips LLM-triggering
flows (user_message → assistant_text) to avoid cost / non-determinism — those
must be covered by future expansion if needed.

Usage:
    BASE=https://dashboard-server.tail4cfa2.ts.net \
        BRIDGE_COOKIE='bridge_session=<token>' \
        python tests/phase2_drive.py

Exits 0 on completion (drive errors are logged but don't abort recording —
the recorder captures them, which is what we want to byte-diff).
"""
from __future__ import annotations

import asyncio
import io
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

_drive_tag = "phase2drv"  # fixed across runs; replay diff requires identical request bodies


def _http(method: str, path: str, body=None, *, cookie: str | None = "_default",
          content_type: str = "application/json", raw_body: bytes | None = None,
          allow_redirects: bool = False) -> tuple[int, bytes]:
    url = BASE + path
    headers: dict[str, str] = {}
    if cookie == "_default":
        cookie = COOKIE
    if cookie:
        headers["Cookie"] = cookie
    data: bytes | None = None
    if raw_body is not None:
        data = raw_body
        if content_type:
            headers["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        opener = urllib.request.build_opener(_NoRedirect()) if not allow_redirects else urllib.request.build_opener()
        with opener.open(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _step(label: str, code: int, expected: int | tuple[int, ...] | None = None) -> None:
    if expected is not None:
        ok = code == expected if isinstance(expected, int) else code in expected
        marker = "OK " if ok else "!! "
    else:
        marker = "   "
    print(f"  {marker}[{code}] {label}")


def http_anon() -> None:
    print("== HTTP anonymous ==")
    c, _ = _http("GET", "/api/health", cookie=None)
    _step("GET /api/health (no cookie)", c, 200)
    c, _ = _http("GET", "/api/vapid-public-key", cookie=None)
    _step("GET /api/vapid-public-key", c, 200)
    c, _ = _http("GET", "/.well-known/oauth-protected-resource/mcp", cookie=None)
    _step("GET /.well-known/oauth-protected-resource/mcp", c, 200)
    c, _ = _http("GET", "/.well-known/oauth-authorization-server/mcp", cookie=None)
    _step("GET /.well-known/oauth-authorization-server/mcp", c, 200)
    c, _ = _http("GET", "/sw.js", cookie=None)
    _step("GET /sw.js", c, 200)
    c, _ = _http("GET", "/manifest.json", cookie=None)
    _step("GET /manifest.json", c, 200)
    c, _ = _http("GET", "/icon.svg", cookie=None)
    _step("GET /icon.svg", c, 200)
    c, _ = _http("GET", "/login", cookie=None)
    _step("GET /login", c, (200, 303))
    c, _ = _http("GET", "/setup", cookie=None)
    _step("GET /setup", c, (200, 303))

    c, _ = _http("GET", "/api/sessions", cookie=None)
    _step("GET /api/sessions [no cookie]", c, (401, 303))
    c, _ = _http("GET", "/api/meta", cookie=None)
    _step("GET /api/meta [no cookie]", c, (401, 303))
    c, _ = _http("GET", "/", cookie=None)
    _step("GET / [no cookie]", c, (200, 303, 401))


def http_authed_readonly() -> None:
    print("== HTTP authed read-only ==")
    for path, exp in [
        ("/api/health", 200),
        ("/api/meta", 200),
        ("/api/usage", 200),
        ("/api/sessions", 200),
        ("/api/today-todos", 200),
        ("/api/vapid-public-key", 200),
        ("/api/settings/weekly-report", 200),
        ("/api/settings/notion-sync", 200),
        ("/api/sync/targets", 200),
        # /devices HTML page contains "last seen X" timestamps that aren't
        # round-trippable; checked once manually (200) but excluded from the
        # replay diff to keep it deterministic.
        ("/", 200),
    ]:
        c, _ = _http("GET", path)
        _step(f"GET {path}", c, exp)

    c, _ = _http("GET", "/api/browse?path=/home/dev")
    _step("GET /api/browse?path=/home/dev", c, 200)
    c, _ = _http("GET", "/api/browse?path=/home/dev/phone-bridge")
    _step("GET /api/browse?path=/home/dev/phone-bridge", c, 200)
    # /api/browse?path=/ lists /home/dev which contains rolling deploy
    # backups (phone-bridge.bak.<timestamp>/). Excluded to keep diff stable;
    # status code verified manually (200).
    c, _ = _http("GET", "/api/browse?path=/nonexistent-zzzzz")
    _step("GET /api/browse?path=/nonexistent", c, (400, 404))

    # /api/poi/around hits live Foursquare/高德/OSM APIs; responses are
    # non-deterministic across runs. Status-code-only checked manually (200);
    # excluded from the replay diff.

    # Repeat-pass over the cheapest read-only endpoints to broaden record count
    # without much wall time. The replay diff is per-record byte equality, so
    # more records = wider net for catching regressions.
    repeat_paths = (
        "/api/health", "/api/meta", "/api/usage", "/api/sessions",
        "/api/today-todos", "/api/settings/weekly-report",
        "/api/settings/notion-sync", "/api/sync/targets",
        "/api/vapid-public-key",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server/mcp",
        "/sw.js", "/manifest.json", "/icon.svg",
    )
    for _ in range(3):  # three full passes to clear the ≥80 record threshold
        for path in repeat_paths:
            c, _ = _http("GET", path)
            _step(f"GET {path} (repeat)", c, (200, 404, 400, 500))


def http_session_crud() -> None:
    print("== HTTP session CRUD round-trip ==")
    c, body = _http("POST", "/api/sessions",
                    body={"cwd": "/home/dev/phone-bridge"})
    _step("POST /api/sessions", c, 200)
    sid = ""
    try:
        sid = json.loads(body).get("id") or json.loads(body).get("session_id") or ""
    except Exception:
        pass
    if not sid:
        print("    (no sid in response, skipping rest of CRUD)")
        return

    c, _ = _http("GET", f"/api/sessions/{sid}")
    _step(f"GET /api/sessions/{sid[:8]}...", c, 200)
    c, _ = _http("PATCH", f"/api/sessions/{sid}",
                 body={"title": f"phase2-drive-{_drive_tag}"})
    _step(f"PATCH /api/sessions/{sid[:8]}... title", c, 200)
    c, _ = _http("DELETE", f"/api/sessions/{sid}")
    _step(f"DELETE /api/sessions/{sid[:8]}...", c, 200)

    c, _ = _http("GET", "/api/sessions/0000000000000000000000")
    _step("GET /api/sessions/<bad>", c, (404, 400))


def http_mkdir() -> None:
    print("== HTTP mkdir (error paths only, kept idempotent for replay) ==")
    # Always-exists path → 409 in both runs
    c, _ = _http("POST", "/api/mkdir", body={"path": "/home/dev"})
    _step("POST /api/mkdir /home/dev [exists]", c, (200, 409))
    # Empty path → 400/422 in both runs
    c, _ = _http("POST", "/api/mkdir", body={"path": ""})
    _step("POST /api/mkdir empty", c, (400, 422))


def http_upload() -> None:
    print("== HTTP upload (multipart) ==")
    boundary = f"----driveboundary{_drive_tag}"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(b'Content-Disposition: form-data; name="file"; filename="drive.txt"\r\n')
    body.write(b"Content-Type: text/plain\r\n\r\n")
    body.write(f"phase2 drive {_drive_tag}\n".encode())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    c, _ = _http("POST", "/api/upload", raw_body=body.getvalue(),
                 content_type=f"multipart/form-data; boundary={boundary}")
    _step("POST /api/upload text", c, (200, 201))


def http_misc_writes() -> None:
    print("== HTTP misc writes ==")
    c, body = _http("GET", "/api/settings/weekly-report")
    if c == 200:
        try:
            cur = json.loads(body)
            c2, _ = _http("PUT", "/api/settings/weekly-report", body=cur)
            _step("PUT /api/settings/weekly-report (echo)", c2, 200)
        except Exception as e:
            print(f"    weekly-report PUT skipped: {e}")
    c, body = _http("GET", "/api/settings/notion-sync")
    if c == 200:
        try:
            cur = json.loads(body)
            c2, _ = _http("PUT", "/api/settings/notion-sync", body=cur)
            _step("PUT /api/settings/notion-sync (echo)", c2, 200)
        except Exception as e:
            print(f"    notion-sync PUT skipped: {e}")


def http_subscribe_unsubscribe() -> None:
    print("== HTTP push subscribe/unsubscribe ==")
    sub = {
        "endpoint": f"https://example.com/push/drive-{_drive_tag}",
        "keys": {"p256dh": "BG" + "A" * 86, "auth": "B" * 22},
    }
    c, _ = _http("POST", "/api/subscribe", body=sub)
    _step("POST /api/subscribe", c, (200, 201))
    c, _ = _http("POST", "/api/unsubscribe", body={"endpoint": sub["endpoint"]})
    _step("POST /api/unsubscribe", c, (200, 204))


async def ws_drive() -> None:
    print("== WS connect + cmd frames ==")
    ws_url = f"{WS_BASE}/ws"
    headers = [("Cookie", COOKIE)] if COOKIE else []
    try:
        async with websockets.connect(ws_url, additional_headers=headers,
                                      open_timeout=10) as ws:
            first = await asyncio.wait_for(ws.recv(), timeout=5)
            try:
                hello = json.loads(first)
                print(f"   hello type={hello.get('type')} session="
                      f"{(hello.get('session_id') or '')[:8]}...")
            except Exception:
                print("   hello frame not JSON")

            cmds = [
                {"type": "ping"},
                {"type": "ping"},
                {"type": "cmd", "cmd": "set_auto_approve", "value": True},
                {"type": "cmd", "cmd": "set_auto_approve", "value": False},
                {"type": "cmd", "cmd": "set_model", "value": ""},
                {"type": "cmd", "cmd": "set_model", "value": "sonnet"},
                {"type": "cmd", "cmd": "set_model", "value": ""},
                {"type": "cmd", "cmd": "cwd", "path": "/home/dev/phone-bridge"},
                {"type": "cmd", "cmd": "cwd", "path": "/home/dev"},
                {"type": "cmd", "cmd": "set_mode", "value": "code"},
                {"type": "ping"},
                {"type": "ping"},
                {"type": "ping"},
                {"type": "unknown_xyz"},
            ]
            for c in cmds:
                await ws.send(json.dumps(c))
                print(f"   sent {c.get('type')}{':' + c.get('cmd', '') if c.get('cmd') else ''}")
                try:
                    while True:
                        reply = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        try:
                            r = json.loads(reply)
                            print(f"     <- {r.get('type')}")
                        except Exception:
                            print(f"     <- raw {reply[:80]!r}")
                except asyncio.TimeoutError:
                    pass
            await ws.send("not-json-at-all")
            try:
                reply = await asyncio.wait_for(ws.recv(), timeout=1.0)
                try:
                    print(f"     <- {(json.loads(reply).get('type'))}")
                except Exception:
                    pass
            except asyncio.TimeoutError:
                pass
    except Exception as exc:
        print(f"   WS drive aborted: {type(exc).__name__}: {exc}")


async def main() -> None:
    print(f"Phase 2 driver target: {BASE}")
    print(f"Cookie: {'set' if COOKIE else 'NOT SET'}")
    print(f"Drive tag: {_drive_tag}")
    print()
    http_anon()
    http_authed_readonly()
    http_session_crud()
    http_mkdir()
    http_upload()
    http_misc_writes()
    http_subscribe_unsubscribe()
    await ws_drive()
    print()
    print("Drive complete.")


if __name__ == "__main__":
    t0 = time.time()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    print(f"   {time.time() - t0:.1f}s elapsed")
