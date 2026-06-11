"""HTTP auth middleware — three outcomes, no hints.

Every request resolves to exactly one of:
  1. Always-public infra path (/api/health, OAuth well-known) → pass through.
  2. First path segment matches the super-link secret → dispatch to the gate
     (app.auth.gate.superlink_gate). The secret IS the path; there is no
     telltale /login or /setup URL visible to outside observers.
  3. Valid device cookie → real app, with sliding-cookie refresh.
  4. Everything else → misleading nginx-style 503 decoy. No redirect, no
     login form, no hint that Phone Bridge or any auth system exists here.

`_current_device` is a stable public helper imported by app.auth.pages and
app.api.meta — its name, location, and signature must remain unchanged.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

import auth as auth_mod

from app.auth.gate import superlink_gate
from app.auth.state import _COOKIE_SECONDS, auth_state

# Only these stay reachable WITHOUT a device cookie:
#  - /api/health: the deploy tool health-checks it over the public Funnel URL.
#  - the two RFC well-known OAuth paths: claude.ai's connector probes them
#    unauthenticated during OAuth discovery for the sibling mcp_pb service.
_PUBLIC_EXACT = {
    "/api/health",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server/mcp",
    # PB hooks on the same VM POST here to trigger pushes. The push.py
    # endpoint enforces request.client.host in {127.0.0.1, ::1, localhost}
    # so adding it to the allowlist does NOT expose it to the public —
    # Tailscale Serve / nginx never proxies loopback-bound peers here.
    "/api/push/send",
}

# Generic nginx-style 503 — misdirects scanners toward "backend is down",
# revealing nothing about Phone Bridge or that an auth system exists here.
_DECOY_BODY = (
    b"<html>\r\n<head><title>503 Service Temporarily Unavailable</title></head>\r\n"
    b"<body>\r\n<center><h1>503 Service Temporarily Unavailable</h1></center>\r\n"
    b"<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)


def decoy_response() -> Response:
    return Response(content=_DECOY_BODY, status_code=503,
                    media_type="text/html", headers={"Retry-After": "120"})


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

    # 1. Always-public infra (health + OAuth discovery).
    if path in _PUBLIC_EXACT:
        return await call_next(request)

    # 2. The hidden gate: first path segment matches the super-link secret.
    #    The secret IS the path — there is no telltale auth path.
    seg = path.lstrip("/").split("/", 1)[0]
    if seg and auth_state.verify_super_link(seg):
        return await superlink_gate(request)

    # 3. Trusted device → the real app, with sliding-cookie refresh.
    device = _current_device(request)
    if device is not None:
        request.state.device = device
        response = await call_next(request)
        token = request.cookies.get(auth_mod.COOKIE_NAME)
        if token:
            auth_mod.set_session_cookie(response, token, max_age=_COOKIE_SECONDS)
        return response

    # 4. Everything else → misleading decoy. No redirect, no login form, no hint.
    return decoy_response()
