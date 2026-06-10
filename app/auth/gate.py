"""The hidden super-link auth gate.

Reached ONLY when the request's first path segment matches the stored
super-link hash (the middleware dispatches here). GET renders the password +
TOTP form; POST verifies and, on success, enrols the current device (issues a
session cookie) and redirects into the app. The form posts back to the same
secret path, so no telltale auth path is ever exposed.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

import auth as auth_mod

from app.auth.state import _COOKIE_SECONDS, auth_state
from app.auth.views import _page, _ua_short


def _form(path: str, *, error: str = "") -> HTMLResponse:
    err = f'<p class="error">{error}</p>' if error else ""
    return _page("Sign in", f"""
<h1>Sign in</h1>
<p class="sub">Enter password and the 6-digit code from your authenticator.</p>
<form method="post" action="{path}">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" required autofocus autocomplete="current-password">
  <label for="code">6-digit code</label>
  <input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required autocomplete="one-time-code">
  <label for="device_name">Name this device (optional)</label>
  <input id="device_name" name="device_name" type="text" maxlength="40">
  <button type="submit">Sign in</button>
</form>{err}""", status=200 if not error else 401)


async def superlink_gate(request: Request):
    """Dispatched by the middleware for a path matching the super-link hash."""
    path = request.url.path
    if request.method == "GET":
        return _form(path)

    # POST — verify credentials
    ip = auth_mod.client_ip(request)
    allowed, retry_after = auth_state.can_attempt(ip)
    if not allowed:
        return _form(path, error=f"Too many attempts. Try again in {retry_after}s.")
    form = await request.form()
    password = str(form.get("password", ""))
    code = str(form.get("code", ""))
    device_name = str(form.get("device_name", ""))
    if not (auth_state.verify_password(password) and auth_state.verify_totp(code)):
        auth_state.record_fail(ip)
        return _form(path, error="Invalid password or code.")
    auth_state.clear_fails(ip)
    name = (device_name.strip() or _ua_short(request))[:40]
    token = auth_state.issue_device_token(
        name=name, ip=ip, ua=request.headers.get("user-agent", ""),
    )
    resp = RedirectResponse("/", status_code=303)
    auth_mod.set_session_cookie(resp, token, max_age=_COOKIE_SECONDS)
    return resp
