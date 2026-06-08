"""Auth state singleton — the persistent `AuthState` for password/TOTP login.

`_AUTH_FILE` resolves to `settings.bridge_auth_file` if set, else
`<repo-root>/.bridge_auth.json` (matches the historical default — the file
lives at repo root, NOT under .bridge_data/).

`auth_state` is created at module import. The same instance is shared by
the middleware, the auth pages, and the WS handler's cookie check.
"""
from __future__ import annotations

from pathlib import Path

import auth as auth_mod

from app.paths import BRIDGE_ROOT
from app.settings import settings

_AUTH_FILE: Path = Path(settings.bridge_auth_file) if settings.bridge_auth_file else (
    BRIDGE_ROOT / ".bridge_auth.json"
)
_COOKIE_DAYS: int = settings.bridge_cookie_days
_COOKIE_SECONDS: int = _COOKIE_DAYS * 86400

auth_state: auth_mod.AuthState = auth_mod.AuthState(_AUTH_FILE)
