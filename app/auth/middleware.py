"""HTTP auth middleware + supporting predicates.

Public routes (login/setup/static/known service-discovery endpoints) bypass
auth entirely. Everything else requires a valid `bridge_session` cookie. If
auth isn't initialized yet, HTML clients get redirected to `/setup`; JSON
clients get a 503. If unauthed, HTML → `/login` redirect; JSON → 401.

Cookie sliding expiry: every authed response re-stamps the cookie for
another `_COOKIE_SECONDS` window.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

import auth as auth_mod

from app.auth.state import _COOKIE_SECONDS, auth_state

_PUBLIC_PREFIXES = ("/login", "/logout", "/setup", "/static/")
_PUBLIC_EXACT = {
    "/sw.js", "/manifest.json", "/icon.svg",
    "/api/health", "/api/vapid-public-key",
    # RFC 9728 OAuth protected-resource metadata for the mcp_pb sibling service.
    # Phone-bridge owns the root-path Tailscale Funnel mapping; mcp_pb's
    # public URL is /mcp on the same hostname. claude.ai's connector probes
    # this well-known URL during OAuth discovery before doing DCR.
    "/.well-known/oauth-protected-resource/mcp",
    # RFC 8414 path-suffixed authorization-server metadata. claude.ai's
    # connector tries this URL (not /mcp/.well-known/...) to find OAuth endpoints.
    "/.well-known/oauth-authorization-server/mcp",
}


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for p in _PUBLIC_PREFIXES:
        base = p.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


def _current_device(request: Request) -> dict | None:
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if not token:
        return None
    return auth_state.lookup_token(
        token,
        ip=auth_mod.client_ip(request),
        ua=request.headers.get("user-agent", ""),
    )


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    # Not initialized yet → force first-time setup
    if not auth_state.is_initialized():
        if _wants_html(request):
            return RedirectResponse("/setup", status_code=303)
        return JSONResponse({"error": "not initialized"}, status_code=503)

    device = _current_device(request)
    if device is None:
        if _wants_html(request):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    request.state.device = device
    response = await call_next(request)
    # Sliding expiry: every authed request renews the cookie for another N days.
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if token:
        auth_mod.set_session_cookie(response, token, max_age=_COOKIE_SECONDS)
    return response
