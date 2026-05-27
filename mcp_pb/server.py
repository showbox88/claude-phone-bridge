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


_load_env(os.environ.get("MCP_PB_ENV", "/home/dev/phone-bridge/.env"))

PB_URL          = os.environ.get("POCKETBASE_URL", "http://127.0.0.1:8090").rstrip("/")
PB_EMAIL        = os.environ.get("POCKETBASE_ADMIN_EMAIL", "")
PB_PASSWORD     = os.environ.get("POCKETBASE_ADMIN_PASSWORD", "")
LISTEN_HOST     = os.environ.get("MCP_PB_HOST", "127.0.0.1")
LISTEN_PORT     = int(os.environ.get("MCP_PB_PORT", "8091"))
PUBLIC_URL      = os.environ.get(
    "MCP_PB_PUBLIC_URL",
    "https://dashboard-server.tail4cfa2.ts.net/mcp",
).rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("mcp_pb")


# ---------------------------------------------------------------------------
# PocketBase HTTP helpers
# ---------------------------------------------------------------------------
def _http(method: str, url: str, body: Any | None = None, headers: dict | None = None,
          timeout: float = 15.0) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:500]}


_pb_token: str | None = None
_pb_token_expiry: float = 0.0

def _pb_auth() -> str:
    global _pb_token, _pb_token_expiry
    if _pb_token and time.time() < _pb_token_expiry:
        return _pb_token
    code, data = _http("POST", f"{PB_URL}/api/collections/_superusers/auth-with-password",
                       body={"identity": PB_EMAIL, "password": PB_PASSWORD},
                       headers={"Content-Type": "application/json"})
    if code != 200:
        raise RuntimeError(f"PB auth failed: {code} {data}")
    _pb_token = data["token"]
    _pb_token_expiry = time.time() + 25 * 60
    return _pb_token


def _pb(method: str, path: str, body: Any | None = None) -> Any:
    code, data = _http(method, f"{PB_URL}{path}", body=body, headers={
        "Authorization": _pb_auth(),
        "Content-Type": "application/json",
    })
    if code >= 400:
        raise RuntimeError(f"PB {method} {path}: {code} {data}")
    return data


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
@mcp.tool()
def pb_list_collections() -> dict:
    """List all PocketBase collections with their fields and (for select fields)
    valid values. Call this at the start of a Smart Note conversation so you
    know the current schema and pick the right collection / select option.
    """
    data = _pb("GET", "/api/collections?perPage=100")
    out = []
    for c in data.get("items", []):
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


@mcp.tool()
def pb_search(
    collection: str,
    filter: str = "",
    sort: str = "-created",
    expand: str = "",
    page: int = 1,
    per_page: int = 30,
) -> dict:
    """Search records in a PocketBase collection.

    Filter uses PB DSL: `(field='value' && other!=0)`. Examples:
      - status='Active' && priority='High'
      - title~'idea'           (~ = LIKE)
      - date >= '2026-01-01'

    Sort: comma list with `-` prefix for desc. e.g. '-date,title'.
    Expand: comma list of relation field names whose target records you want embedded.
    """
    params = []
    if filter:
        params.append("filter=" + urllib.parse.quote(filter, safe=""))
    if sort:
        params.append("sort=" + urllib.parse.quote(sort, safe=",-"))
    if expand:
        params.append("expand=" + urllib.parse.quote(expand, safe=","))
    params.append(f"page={int(page)}")
    params.append(f"perPage={min(max(int(per_page), 1), 200)}")
    return _pb("GET", f"/api/collections/{collection}/records?" + "&".join(params))


@mcp.tool()
def pb_get(collection: str, id: str, expand: str = "") -> dict:
    """Get a single record by ID, optionally with `expand` for relations."""
    q = "?expand=" + urllib.parse.quote(expand, safe=",") if expand else ""
    return _pb("GET", f"/api/collections/{collection}/records/{id}{q}")


@mcp.tool()
def pb_create(collection: str, data: dict) -> dict:
    """Create a record in `collection`. `data` is a field map.

    PB auto-fills id, created, updated. For select fields use the exact string
    value (case-sensitive). For relation fields use the target record's id
    (single) or list of ids (multi).
    """
    return _pb("POST", f"/api/collections/{collection}/records", body=data)


@mcp.tool()
def pb_update(collection: str, id: str, data: dict) -> dict:
    """Update specific fields of a record. Pass only fields to change.

    Common patterns:
      - Archive: pb_update(coll, id, {"status": "Archived"})
      - Mark todo done: pb_update("todos", id, {"status": "Done", "completed_at": "2026-05-27"})
    """
    return _pb("PATCH", f"/api/collections/{collection}/records/{id}", body=data)


@mcp.tool()
def smartnote_open_context() -> dict:
    """Fetch active high-priority memos from `claude_memos`. Call at the start
    of a Smart Note conversation to recover persistent context.
    """
    f = urllib.parse.quote("status='Active' && priority='High'", safe="")
    return _pb("GET",
        f"/api/collections/claude_memos/records?filter={f}&sort=-date&perPage=50")


# ---------------------------------------------------------------------------
# Wire up & run
# ---------------------------------------------------------------------------
app = mcp.streamable_http_app()


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
