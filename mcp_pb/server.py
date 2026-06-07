#!/usr/bin/env python3
"""PocketBase MCP server for claude.ai Custom Connectors.

Exposes 5 generic CRUD tools + 1 domain helper over PocketBase. claude.ai
calls these over HTTPS through Tailscale Funnel.

Auth: OAuth 2.0 with Dynamic Client Registration. claude.ai's Custom
Connector UI does the DCR flow automatically — leave both OAuth Client ID
and Client Secret fields blank in the UI. The provider is single-tenant
and auto-approves all authorize requests (no user-interactive login).
Tokens live in process memory; restart wipes state and claude.ai re-registers
transparently.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse


# ---------------------------------------------------------------------------
# Env / config
# ---------------------------------------------------------------------------
def _load_env(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# MCP_PB_ENV intentionally stays on os.environ: _load_env must run BEFORE
# Settings is imported (it populates env from the .env file Settings then
# reads). Phase 1 cleanup considers replacing _load_env entirely with
# pydantic-settings' built-in .env support.
_load_env(os.environ.get("MCP_PB_ENV", "/home/dev/phone-bridge/.env"))

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from app.settings import settings  # noqa: E402

PB_URL          = settings.pocketbase_url or "http://127.0.0.1:8090"
PB_EMAIL        = settings.pocketbase_admin_email
PB_PASSWORD     = settings.pocketbase_admin_password
LISTEN_HOST     = settings.mcp_pb_host
LISTEN_PORT     = settings.mcp_pb_port
PUBLIC_URL      = (settings.mcp_pb_public_url
                   or "https://dashboard-server.tail4cfa2.ts.net/mcp").rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("mcp_pb")


# ---------------------------------------------------------------------------
# PocketBase HTTP client
# ---------------------------------------------------------------------------
# Phase 1: replaced bespoke _http / _pb_auth / _pb (~40 lines) with the
# unified app.integrations.pb.PBClient. The client carries its own
# per-instance token cache + 5xx/429/401 retry logic.

from app.agent.mcp_tools.prompts import TOOL_DESCRIPTIONS  # noqa: E402
from app.integrations.pb import PBClient  # noqa: E402

_pb_client: PBClient | None = None


def _pb() -> PBClient:
    global _pb_client
    if _pb_client is None:
        _pb_client = PBClient(PB_URL, PB_EMAIL, PB_PASSWORD)
    return _pb_client


# ---------------------------------------------------------------------------
# OAuth 2.0 provider — single-tenant, auto-approve, in-memory state
# ---------------------------------------------------------------------------
class InMemoryOAuthProvider(OAuthAuthorizationServerProvider):
    """Bare-minimum OAuth 2.0 implementation for a personal MCP server.

    - DCR (Dynamic Client Registration) lets claude.ai self-register
    - /authorize auto-approves (no user UI; we trust whoever has DCR access)
    - Standard authorization-code-with-PKCE flow
    - Access tokens are random url-safe strings, kept in memory
    - Refresh tokens supported but optional

    State resets on process restart; claude.ai re-runs DCR transparently.
    """
    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # RFC 7591 §3.2.1 makes `client_secret_expires_at` REQUIRED when a
        # client_secret is issued (0 = never expires). The SDK leaves it None
        # unless ClientRegistrationOptions.client_secret_expiry_seconds is
        # set, but None serializes as omitted — and claude.ai (rightly)
        # rejects the registration response in that case with
        # "Couldn't register with sign-in service".
        #
        # We mutate the inbound object in-place; the SDK's RegistrationHandler
        # serializes this same object as the 201 body, so the fix lands in
        # the wire response.
        if client_info.client_secret and client_info.client_secret_expires_at is None:
            client_info.client_secret_expires_at = 0
        self.clients[client_info.client_id] = client_info
        log.info("OAuth: registered client %s", client_info.client_id[:8])

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        # Auto-approve: skip any user-interactive step.
        code = secrets.token_urlsafe(32)
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 600,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        log.info("OAuth: issued auth code for client %s", client.client_id[:8])
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state,
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> Optional[AuthorizationCode]:
        ac = self.auth_codes.get(authorization_code)
        if ac and ac.expires_at < time.time():
            self.auth_codes.pop(authorization_code, None)
            return None
        return ac

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        self.auth_codes.pop(authorization_code.code, None)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        self.access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + 3600,
            resource=authorization_code.resource,
        )
        self.refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + 86400 * 30,
        )
        log.info("OAuth: issued access+refresh tokens for client %s", client.client_id[:8])
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=refresh,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> Optional[RefreshToken]:
        rt = self.refresh_tokens.get(refresh_token)
        if rt and rt.expires_at and rt.expires_at < time.time():
            self.refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self.refresh_tokens.pop(refresh_token.token, None)
        access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        granted = scopes or refresh_token.scopes
        self.access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=granted,
            expires_at=now + 3600,
            resource=None,
        )
        self.refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=refresh_token.scopes,
            expires_at=now + 86400 * 30,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(granted) if granted else None,
            refresh_token=new_refresh,
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        at = self.access_tokens.get(token)
        if at and at.expires_at and at.expires_at < time.time():
            self.access_tokens.pop(token, None)
            return None
        return at

    async def revoke_token(
        self,
        client: OAuthClientInformationFull,
        token: AccessToken | RefreshToken,
    ) -> None:
        if isinstance(token, AccessToken):
            self.access_tokens.pop(token.token, None)
        elif isinstance(token, RefreshToken):
            self.refresh_tokens.pop(token.token, None)


# ---------------------------------------------------------------------------
# Allowed hosts/origins for transport security (DNS rebinding protection)
# ---------------------------------------------------------------------------
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get(
        "MCP_PB_ALLOWED_HOSTS",
        "dashboard-server.tail4cfa2.ts.net,"
        "dashboard-server.tail4cfa2.ts.net:443,"
        "127.0.0.1:*,localhost:*"
    ).split(",") if h.strip()
]
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "MCP_PB_ALLOWED_ORIGINS",
        "https://dashboard-server.tail4cfa2.ts.net,"
        "http://127.0.0.1:*,http://localhost:*"
    ).split(",") if o.strip()
]


# ---------------------------------------------------------------------------
# Configure FastMCP with the OAuth provider
# ---------------------------------------------------------------------------
provider = InMemoryOAuthProvider()
mcp = FastMCP(
    "pocketbase",
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_URL),
        resource_server_url=AnyHttpUrl(PUBLIC_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=["mcp"],
    ),
)
mcp.settings.transport_security.allowed_hosts = ALLOWED_HOSTS
mcp.settings.transport_security.allowed_origins = ALLOWED_ORIGINS
# Tailscale Funnel strips `/mcp` before forwarding; serve MCP at app root.
mcp.settings.streamable_http_path = "/"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool(description=TOOL_DESCRIPTIONS["pb_list_collections"])
def pb_list_collections() -> dict:
    cols = _pb().list_collections()
    out = []
    for c in cols:
        if c.get("type") != "base":
            continue
        fields = []
        for f in c.get("fields", []):
            fdesc: dict[str, Any] = {"name": f["name"], "type": f["type"]}
            if f["type"] == "select":
                fdesc["values"]    = f.get("values", [])
                fdesc["maxSelect"] = f.get("maxSelect", 1)
            if f["type"] == "relation":
                fdesc["target"]    = f.get("collectionId")
                fdesc["maxSelect"] = f.get("maxSelect", 1)
            if f.get("required"):
                fdesc["required"] = True
            fields.append(fdesc)
        out.append({"name": c["name"], "id": c["id"], "fields": fields})
    return {"collections": out}


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_search"])
def pb_search(
    collection: str,
    filter: str = "",
    sort: str = "-created",
    expand: str = "",
    page: int = 1,
    per_page: int = 30,
) -> dict:
    return _pb().list_page(
        collection, filter=filter, sort=sort, expand=expand,
        page=int(page), per_page=min(max(int(per_page), 1), 200),
    )


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_get"])
def pb_get(collection: str, id: str, expand: str = "") -> dict:
    return _pb().get_record(collection, id, expand=expand)


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_create"])
def pb_create(collection: str, data: dict) -> dict:
    return _pb().create_record(collection, data)


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_update"])
def pb_update(collection: str, id: str, data: dict) -> dict:
    return _pb().update_record(collection, id, data)


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_delete"])
def pb_delete(collection: str, id: str) -> dict:
    _pb().delete_record(collection, id)
    return {"ok": True, "collection": collection, "deleted": id}


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_create_collection"])
def pb_create_collection(name: str, fields: list, type: str = "base") -> dict:
    return _pb().create_collection({"name": name, "type": type, "fields": fields})


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_update_collection"])
def pb_update_collection(id_or_name: str, patch: dict) -> dict:
    return _pb().update_collection(id_or_name, patch)


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_delete_collection"])
def pb_delete_collection(id_or_name: str) -> dict:
    _pb().delete_collection(id_or_name)
    return {"ok": True, "deleted": id_or_name}


@mcp.tool(description=TOOL_DESCRIPTIONS["pb_get_collection"])
def pb_get_collection(id_or_name: str) -> dict:
    return _pb().get_collection(id_or_name)


@mcp.tool(description=TOOL_DESCRIPTIONS["smartnote_open_context"])
def smartnote_open_context() -> dict:
    return _pb().list_page(
        "claude_memos",
        filter="status='Active' && priority='High'",
        sort="-date", per_page=50,
    )


# ---------------------------------------------------------------------------
# Wire up & run
# ---------------------------------------------------------------------------
app = mcp.streamable_http_app()


# Debug middleware: log the full request/response body for /register so we
# can see exactly what claude.ai is sending and what we return.
from starlette.middleware.base import BaseHTTPMiddleware
class RegisterDebugLogger(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path != "/register":
            return await call_next(request)
        body = await request.body()
        log.info("REGISTER REQ from %s body=%s", request.client.host if request.client else "?", body.decode("utf-8", "replace")[:1000])
        # Re-attach body since reading consumed it
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = receive
        resp = await call_next(request)
        # Read response body
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c)
        resp_body = b"".join(chunks)
        log.info("REGISTER RESP status=%d body=%s", resp.status_code, resp_body.decode("utf-8", "replace")[:1000])
        from starlette.responses import Response as StResponse
        return StResponse(content=resp_body, status_code=resp.status_code, headers=dict(resp.headers), media_type=resp.media_type)

app.add_middleware(RegisterDebugLogger)


async def health(request: Request):  # noqa: ARG001
    return JSONResponse({"ok": True, "server": "mcp_pb"})


async def oauth_protected_resource(request: Request):  # noqa: ARG001
    """RFC 9728: lets a client discover which authorization server protects
    this MCP resource. claude.ai probes this before doing OAuth."""
    return JSONResponse({
        "resource": PUBLIC_URL,
        "authorization_servers": [PUBLIC_URL],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    })


app.add_route("/health", health, methods=["GET"])
app.add_route("/.well-known/oauth-protected-resource",
              oauth_protected_resource, methods=["GET"])


if __name__ == "__main__":
    import uvicorn
    log.info("starting mcp_pb on %s:%d (PB=%s, public=%s)",
             LISTEN_HOST, LISTEN_PORT, PB_URL, PUBLIC_URL)
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
