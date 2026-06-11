"""Verify /api/push/send passes auth middleware without a device cookie."""
from app.auth.middleware import _PUBLIC_EXACT


def test_push_send_is_public_exact():
    assert "/api/push/send" in _PUBLIC_EXACT
