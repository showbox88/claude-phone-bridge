"""Auth pages — /logout, /devices.

/setup and /login have been removed — first-time setup is a CLI task and
the only way to enrol a new device is via the hidden super-link gate
(app.auth.gate). These routes are kept (all require a valid device cookie,
enforced by the middleware before reaching here):

  POST/GET /logout      — revoke the current device's session
  GET      /devices     — list all enrolled devices
  POST     /devices/revoke — revoke any device by hash

All routes are mounted on `pages_router`; server.py / app.main
include_router()s it.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

import auth as auth_mod

from app.auth.middleware import _current_device
from app.auth.state import auth_state
from app.auth.views import _page, _html_escape

pages_router = APIRouter()


# ---------- /logout ----------

@pages_router.post("/logout")
@pages_router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if token:
        h = auth_mod._hash_token(token)
        auth_state.revoke(h)
    resp = RedirectResponse("/", status_code=303)
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
        resp = RedirectResponse("/", status_code=303)
        auth_mod.clear_session_cookie(resp)
        return resp
    return RedirectResponse("/devices", status_code=303)
