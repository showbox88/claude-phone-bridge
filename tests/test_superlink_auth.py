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
