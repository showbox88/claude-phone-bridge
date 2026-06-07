"""RFC 8414/9728 OAuth discovery for the mcp_pb sibling service.

Phone-bridge owns the root-path Tailscale Funnel mapping; mcp_pb lives at
/mcp on the same hostname. claude.ai's Custom Connector probes both of
these well-known endpoints before doing DCR.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/.well-known/oauth-protected-resource/mcp")
async def mcp_oauth_resource_metadata():
    return {
        "resource": "https://dashboard-server.tail4cfa2.ts.net/mcp",
        "authorization_servers": ["https://dashboard-server.tail4cfa2.ts.net/mcp"],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-authorization-server/mcp")
async def mcp_oauth_authorization_server_metadata():
    base = "https://dashboard-server.tail4cfa2.ts.net/mcp"
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        "revocation_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    }
