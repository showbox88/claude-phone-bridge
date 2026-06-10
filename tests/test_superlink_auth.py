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
