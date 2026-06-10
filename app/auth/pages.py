"""Auth pages — /setup, /login, /logout, /devices.

All routes are mounted on `pages_router`, an APIRouter that server.py
include_router()s. Each handler is verbatim from server.py's old
auth-pages block; only the decorator target (router instead of app)
and some helper imports changed.
"""
from __future__ import annotations

import datetime as _dt
import socket

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

import auth as auth_mod

from app.auth.middleware import _current_device
from app.auth.state import _COOKIE_SECONDS, auth_state
from app.auth.views import _AUTH_PAGE_CSS, _page, _ua_short, _html_escape  # noqa: F401
from app.settings import settings

pages_router = APIRouter()


# ---------- /setup (first-time only) ----------

@pages_router.get("/setup")
async def setup_get():
    if auth_state.is_initialized():
        # Stage 2 path — they may be mid-flow if no device exists yet
        if not auth_state.list_devices():
            return RedirectResponse("/setup/verify", status_code=303)
        return RedirectResponse("/login", status_code=303)
    return _page("First-time setup", """
<h1>First-time setup</h1>
<p class="sub">Set the master password. After this, scan the TOTP QR with your authenticator app.</p>
<form method="post" action="/setup">
  <label for="password">Password (min 12 chars)</label>
  <input id="password" name="password" type="password" minlength="12" required autofocus autocomplete="new-password">
  <label for="password2">Confirm</label>
  <input id="password2" name="password2" type="password" minlength="12" required autocomplete="new-password">
  <button type="submit">Continue</button>
</form>
""")


@pages_router.post("/setup")
async def setup_post(request: Request, password: str = Form(...), password2: str = Form(...)):
    if auth_state.is_initialized():
        return RedirectResponse("/login", status_code=303)
    if password != password2:
        return _page("First-time setup", """<h1>First-time setup</h1>
<p class="error">Passwords don't match. <a href="/setup">Try again</a>.</p>""", status=400)
    if len(password) < 12:
        return _page("First-time setup", """<h1>First-time setup</h1>
<p class="error">Password too short (need at least 12). <a href="/setup">Try again</a>.</p>""", status=400)
    auth_state.initialize(password)
    return RedirectResponse("/setup/verify", status_code=303)


@pages_router.get("/setup/verify")
async def setup_verify_get():
    if not auth_state.is_initialized() or auth_state.list_devices():
        return RedirectResponse("/login", status_code=303)
    secret = auth_state.totp_secret() or ""
    label = settings.bridge_name or socket.gethostname()
    uri = auth_mod.otpauth_uri(secret, label=label, issuer="Phone Bridge")
    qr = auth_mod.qr_svg(uri)
    # Pretty 4-char chunks for manual entry
    pretty_secret = " ".join(secret[i:i+4] for i in range(0, len(secret), 4))
    return _page("Scan TOTP", f"""
<h1>Add 2FA</h1>
<p class="sub">Three ways — pick whichever works:</p>

<p><b>1. On your phone:</b> tap this link, it'll open your Authenticator app and add the entry directly.</p>
<p style="margin:0.6rem 0 1.2rem"><a href="{uri}" style="display:inline-block;padding:0.6rem 1rem;background:#0b0f14;border:1px solid var(--accent);border-radius:8px;text-decoration:none">Open in Authenticator app →</a></p>

<p><b>2. Scan QR with Authenticator:</b></p>
<div class="qr">{qr}</div>

<p><b>3. Manual entry</b> (if scan fails) — in Google Authenticator: <i>+ → Enter a setup key</i></p>
<table style="width:100%;font-size:0.85rem;margin:0.5rem 0">
  <tr><td class="muted" style="padding:0.2rem 0;width:5em">Account</td><td><code>Phone Bridge</code></td></tr>
  <tr><td class="muted" style="padding:0.2rem 0">Key</td><td><code style="font-size:0.95rem">{pretty_secret}</code></td></tr>
  <tr><td class="muted" style="padding:0.2rem 0">Type</td><td>Time-based (TOTP)</td></tr>
</table>

<form method="post" action="/setup/verify" style="margin-top:1.5rem">
  <label for="code">After adding it, enter the current 6-digit code</label>
  <input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required autofocus autocomplete="one-time-code">
  <label for="device_name">This device's name</label>
  <input id="device_name" name="device_name" type="text" placeholder="e.g. Office PC" maxlength="40" value="">
  <button type="submit">Finish setup</button>
</form>
""")


@pages_router.post("/setup/verify")
async def setup_verify_post(
    request: Request,
    code: str = Form(...),
    device_name: str = Form(""),
):
    if not auth_state.is_initialized() or auth_state.list_devices():
        return RedirectResponse("/login", status_code=303)
    if not auth_state.verify_totp(code):
        return _page("Scan TOTP", """<h1>Scan to add 2FA</h1>
<p class="error">Wrong code. <a href="/setup/verify">Try again</a>.</p>""", status=400)
    name = (device_name.strip() or _ua_short(request))[:40]
    token = auth_state.issue_device_token(
        name=name,
        ip=auth_mod.client_ip(request),
        ua=request.headers.get("user-agent", ""),
    )
    resp = RedirectResponse("/", status_code=303)
    auth_mod.set_session_cookie(resp, token, max_age=_COOKIE_SECONDS)
    return resp


# ---------- /login ----------

@pages_router.get("/login")
async def login_get(request: Request):
    if not auth_state.is_initialized():
        return RedirectResponse("/setup", status_code=303)
    if _current_device(request):
        return RedirectResponse("/", status_code=303)
    return _page("Sign in", f"""
<h1>Sign in</h1>
<p class="sub">Phone Bridge — enter password and the 6-digit code from your authenticator.</p>
<form method="post" action="/login">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" required autofocus autocomplete="current-password">
  <label for="code">6-digit code</label>
  <input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required autocomplete="one-time-code">
  <label for="device_name">Name this device (optional)</label>
  <input id="device_name" name="device_name" type="text" maxlength="40" placeholder="e.g. {_ua_short(request)}">
  <button type="submit">Sign in</button>
</form>
""")


@pages_router.post("/login")
async def login_post(
    request: Request,
    password: str = Form(...),
    code: str = Form(...),
    device_name: str = Form(""),
):
    if not auth_state.is_initialized():
        return RedirectResponse("/setup", status_code=303)
    ip = auth_mod.client_ip(request)
    allowed, retry_after = auth_state.can_attempt(ip)
    if not allowed:
        return _page("Sign in", f"""<h1>Sign in</h1>
<p class="error">Too many failed attempts. Try again in {retry_after}s.</p>""", status=429)
    if not (auth_state.verify_password(password) and auth_state.verify_totp(code)):
        auth_state.record_fail(ip)
        return _page("Sign in", """<h1>Sign in</h1>
<p class="error">Invalid password or code. <a href="/login">Try again</a>.</p>""", status=401)
    auth_state.clear_fails(ip)
    name = (device_name.strip() or _ua_short(request))[:40]
    token = auth_state.issue_device_token(
        name=name, ip=ip, ua=request.headers.get("user-agent", ""),
    )
    resp = RedirectResponse("/", status_code=303)
    auth_mod.set_session_cookie(resp, token, max_age=_COOKIE_SECONDS)
    return resp


# ---------- /logout ----------

@pages_router.post("/logout")
@pages_router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if token:
        h = auth_mod._hash_token(token)
        auth_state.revoke(h)
    resp = RedirectResponse("/login", status_code=303)
    auth_mod.clear_session_cookie(resp)
    return resp


# ---------- /devices (manage logged-in devices) ----------

@pages_router.get("/devices")
async def devices_get(request: Request):
    me = _current_device(request)  # already authed by middleware, but useful for "this device" marker
    devs = sorted(auth_state.list_devices(), key=lambda d: d.get("last_seen", 0), reverse=True)
    rows_html = []
    for d in devs:
        last = d.get("last_seen", 0)
        when = _dt.datetime.fromtimestamp(int(last)).strftime("%Y-%m-%d %H:%M") if last else "—"
        ip = d.get("last_ip", "") or "—"
        is_me = me and d["hash"] == me["hash"]
        marker = '<span class="this-device">THIS DEVICE</span>' if is_me else ""
        rows_html.append(f"""<li><div class="row">
  <div><b>{_html_escape(d.get('name','?'))}</b>{marker}<small>{ip} · last seen {when}</small></div>
  <form method="post" action="/devices/revoke">
    <input type="hidden" name="hash" value="{d['hash']}">
    <button class="danger" type="submit">Revoke</button>
  </form>
</div></li>""")
    body = f"""
<h1>Logged-in devices</h1>
<p class="sub">Revoke any device to log it out immediately.</p>
<ul class="devices">{''.join(rows_html) or '<li class="muted">No devices.</li>'}</ul>
<p style="margin-top:1.2rem"><a href="/">← back</a> · <a href="/logout">log out this device</a></p>
"""
    return _page("Devices", body)


@pages_router.post("/devices/revoke")
async def devices_revoke(request: Request, hash: str = Form(...)):
    me = _current_device(request)
    auth_state.revoke(hash)
    if me and me["hash"] == hash:
        resp = RedirectResponse("/login", status_code=303)
        auth_mod.clear_session_cookie(resp)
        return resp
    return RedirectResponse("/devices", status_code=303)
