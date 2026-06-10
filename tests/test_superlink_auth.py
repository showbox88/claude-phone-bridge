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


def test_view_helpers_importable_from_views():
    from app.auth.views import _page, _AUTH_PAGE_CSS, _ua_short, _html_escape
    html = _page("T", "<p>x</p>")
    assert html.status_code == 200
    assert _html_escape("<a>") == "&lt;a&gt;"


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
    import app.auth.gate as gate
    st = _fresh_state(tmp_path)
    monkeypatch.setattr(gate, "auth_state", st)
    req = _post_request("/" + st.set_super_link(),
                        b"password=wrong&code=000000&device_name=x")
    resp = asyncio.run(gate.superlink_gate(req))
    assert resp.status_code == 401
    assert not st.list_devices()  # no device enrolled on failure


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


def test_cli_rotate_link_sets_verifiable_secret(tmp_path, monkeypatch, capsys):
    st = _fresh_state(tmp_path)
    import app.auth.cli as cli
    monkeypatch.setattr(cli, "auth_state", st, raising=False)
    cli.main(["rotate-link"])
    out = capsys.readouterr().out.strip()
    # the printed secret (last whitespace token containing '/') verifies
    printed = [w for line in out.splitlines() for w in line.split() if "/" in w]
    secret = printed[-1].rsplit("/", 1)[-1]
    assert st.verify_super_link(secret) is True


def test_super_link_path_injection_is_escaped(tmp_path, monkeypatch):
    """A crafted /<secret>/"><script> path must not reflect unescaped into the
    gate form action (the middleware matches on the first segment only)."""
    app, st = _app_with_state(tmp_path, monkeypatch)
    secret = st.set_super_link()
    client = TestClient(app, follow_redirects=False)
    r = client.get(f'/{secret}/"><script>alert(1)</script>')
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text          # not reflected raw
    assert "&lt;script&gt;alert(1)" in r.text         # escaped instead


def test_server_sees_external_super_link_without_restart(tmp_path):
    """A super link minted by a SEPARATE process (the CLI) must be honoured by
    a long-lived server instance without restarting it."""
    path = tmp_path / ".bridge_auth.json"
    server = auth_mod.AuthState(path)
    server.initialize("correct horse battery staple")
    cli = auth_mod.AuthState(path)          # separate process / instance
    secret = cli.set_super_link()
    assert server.verify_super_link(secret) is True
    assert server.has_super_link() is True


def test_server_device_write_does_not_clobber_external_super_link(tmp_path):
    """The server's debounced last_seen write must not overwrite a super link
    that a separate process wrote to the same file (the original clobber bug)."""
    path = tmp_path / ".bridge_auth.json"
    server = auth_mod.AuthState(path)
    server.initialize("correct horse battery staple")
    tok = server.issue_device_token("dev1", ip="1.1.1.1")
    cli = auth_mod.AuthState(path)
    secret = cli.set_super_link()
    server._last_seen_persisted.clear()     # force the debounced persist to fire
    server.lookup_token(tok, ip="2.2.2.2")  # server writes the file
    fresh = auth_mod.AuthState(path)
    assert fresh.verify_super_link(secret) is True   # survived, not clobbered


def test_external_device_enrollment_not_clobbered_by_set_super_link(tmp_path):
    """Reverse direction: the CLI's set_super_link must not drop a device the
    server enrolled after the CLI instance was constructed."""
    path = tmp_path / ".bridge_auth.json"
    server = auth_mod.AuthState(path)
    server.initialize("correct horse battery staple")
    cli = auth_mod.AuthState(path)          # loads state (no devices yet)
    tok = server.issue_device_token("dev1")  # server adds a device afterwards
    cli.set_super_link()                      # must merge, not clobber the device
    fresh = auth_mod.AuthState(path)
    assert fresh.lookup_token(tok) is not None
