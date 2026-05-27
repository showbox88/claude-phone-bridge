#!/usr/bin/env python3
"""PocketBase MCP server for claude.ai's Custom Connectors.

Exposes 5 generic CRUD tools + 1 domain helper. claude.ai calls these over
HTTPS through Tailscale Funnel; we re-auth to local PocketBase and proxy.

Why "generic + schema introspection" instead of one tool per collection:
- New PB collections (or schema changes) automatically show up via the
  pb_list_collections() tool. No code change required.
- The Notion-side Smart Note prompt has 12 select fields with hardcoded
  enums; tomorrow's enum addition won't ripple to claude.ai's prompt if it
  asks pb_list_collections() at conversation start.

Auth model: Bearer token in `Authorization: Bearer <token>` header. The
token is shared with claude.ai's Connector config. Tailscale Funnel handles
HTTPS termination + valid cert. Token is the only thing between the public
internet and your PocketBase, so make it long and random.

Run:  python3 server.py
Or:   systemctl start mcp_pb
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
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
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
MCP_TOKEN       = os.environ.get("MCP_PB_BEARER_TOKEN", "")
LISTEN_HOST     = os.environ.get("MCP_PB_HOST", "127.0.0.1")
LISTEN_PORT     = int(os.environ.get("MCP_PB_PORT", "8091"))

if not MCP_TOKEN:
    raise SystemExit("MCP_PB_BEARER_TOKEN not set — generate one and put in .env")

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
    """Return a valid PB superuser token; cache for ~25 min."""
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
# MCP server + tools
# ---------------------------------------------------------------------------
# FastMCP defaults to DNS-rebinding protection that only accepts localhost.
# Behind Tailscale Funnel the Host header is the funnel hostname, so we
# explicitly whitelist it. Comma-separate in env if you want multiple hosts.
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get(
        "MCP_PB_ALLOWED_HOSTS",
        # The bare hostname (port 443) is what claude.ai will hit through the
        # Tailscale Funnel `/mcp` path. :10000 stays as a fallback for direct
        # curl debugging.
        "dashboard-server.tail4cfa2.ts.net,"
        "dashboard-server.tail4cfa2.ts.net:443,"
        "dashboard-server.tail4cfa2.ts.net:10000,"
        "127.0.0.1:*,localhost:*"
    ).split(",") if h.strip()
]
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "MCP_PB_ALLOWED_ORIGINS",
        "https://dashboard-server.tail4cfa2.ts.net,"
        "https://dashboard-server.tail4cfa2.ts.net:10000,"
        "http://127.0.0.1:*,http://localhost:*"
    ).split(",") if o.strip()
]

mcp = FastMCP("pocketbase")
mcp.settings.transport_security.allowed_hosts = ALLOWED_HOSTS
mcp.settings.transport_security.allowed_origins = ALLOWED_ORIGINS
# Serve MCP at root path. Tailscale Funnel strips the `--set-path=/mcp` prefix
# before forwarding to us, so the public URL `https://host/mcp` arrives here
# as `/`. Make FastMCP's mount match.
mcp.settings.streamable_http_path = "/"


@mcp.tool()
def pb_list_collections() -> dict:
    """List all PocketBase collections with their fields and (for select fields)
    valid values. Call this at the start of a Smart Note conversation so you
    know the current schema and pick the right collection / select option.

    Returns: {collections: [{name, fields: [{name, type, ...}]}]}
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

    Sort: comma list of fields; prefix `-` for desc. e.g. '-date,title'.

    Expand: comma list of relation field names whose target records you want
    embedded (e.g. 'trip,location' on days).

    Returns: {items: [...], page, totalItems, totalPages}
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
        (or {"archived": true} for tables with a checkbox)
      - Mark todo done: pb_update("todos", id, {"status": "Done", "completed_at": "2026-05-27"})
    """
    return _pb("PATCH", f"/api/collections/{collection}/records/{id}", body=data)


@mcp.tool()
def smartnote_open_context() -> dict:
    """Convenience: fetch active high-priority memos from `claude_memos`.
    Call at the start of a Smart Note conversation to recover persistent
    context (decisions, project state, conventions). Equivalent to:
      pb_search('claude_memos', "status='Active' && priority='High'", '-date', '', 1, 50)
    """
    f = urllib.parse.quote("status='Active' && priority='High'", safe="")
    return _pb("GET",
        f"/api/collections/claude_memos/records?filter={f}&sort=-date&perPage=50")


# ---------------------------------------------------------------------------
# Bearer auth middleware
# ---------------------------------------------------------------------------
class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/healthz"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "missing Bearer token"}, status_code=401)
        if not secrets.compare_digest(auth[7:].strip(), MCP_TOKEN):
            log.warning("rejected bearer from %s",
                        request.client.host if request.client else "?")
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Wire up & run
# ---------------------------------------------------------------------------
app = mcp.streamable_http_app()
app.add_middleware(BearerAuth)


async def health(request: Request):  # noqa: ARG001
    return JSONResponse({"ok": True, "server": "mcp_pb"})


app.add_route("/health", health, methods=["GET"])


if __name__ == "__main__":
    import uvicorn
    log.info("starting mcp_pb on %s:%d (PB=%s)", LISTEN_HOST, LISTEN_PORT, PB_URL)
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
