# PocketBase MCP Server (`mcp_pb`)

Exposes PocketBase as a [Model Context Protocol](https://modelcontextprotocol.io) server so that **claude.ai** (the cloud product, via Custom Connectors) can read/write Smart Note data instead of going to Notion.

## Architecture

```
claude.ai project (Anthropic cloud)
  ↓ HTTPS + Bearer token
https://dashboard-server.tail4cfa2.ts.net:10000/mcp       ← Tailscale Funnel (public)
  ↓ proxy
127.0.0.1:8091     ← mcp_pb.service (this directory)
  ↓ HTTP
127.0.0.1:8090     ← PocketBase
```

## Tools exposed

| Tool | Purpose |
|---|---|
| `pb_list_collections()` | Live schema dump — call at start of a Smart Note conversation so Claude knows the current select options |
| `pb_search(coll, filter, sort, expand, page, perPage)` | Paginated search, PB filter DSL |
| `pb_get(coll, id, expand)` | Single record fetch |
| `pb_create(coll, data)` | Create record |
| `pb_update(coll, id, data)` | Patch fields (use for archive too) |
| `smartnote_open_context()` | Convenience: fetch active high-priority `claude_memos` |

## Setup (one-time, already done on dashboard-server)

1. Files synced via `deploy` from local repo → `/home/dev/phone-bridge/mcp_pb/`
2. venv + deps:
   ```bash
   cd /home/dev/phone-bridge/mcp_pb
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. Bearer token generated and appended to `/home/dev/phone-bridge/.env`:
   ```
   MCP_PB_BEARER_TOKEN=<48-byte url-safe random>
   MCP_PB_HOST=127.0.0.1
   MCP_PB_PORT=8091
   ```
4. systemd unit installed:
   ```bash
   sudo cp mcp_pb.service /etc/systemd/system/
   sudo systemctl enable --now mcp_pb
   ```
5. Tailscale Funnel exposes it publicly with auto-HTTPS:
   ```bash
   sudo tailscale funnel --bg --https=10000 http://127.0.0.1:8091
   ```

## Configuring claude.ai

1. Open https://claude.ai → Settings → **Connectors** → **Add custom connector**
2. URL: `https://dashboard-server.tail4cfa2.ts.net:10000/mcp`
3. Authorization: Bearer Token, paste the value of `MCP_PB_BEARER_TOKEN` from the server's `.env`
4. Tools appear in the conversation tool picker
5. Update the Smart Note project's Instructions with [`SMARTNOTE_PROMPT.md`](./SMARTNOTE_PROMPT.md) — drops all Notion-write calls in favor of `pb_*` tools

## Security

- The MCP endpoint is **publicly accessible over the internet** (that's what Tailscale Funnel does — it's how claude.ai's cloud can reach an otherwise tailnet-private service).
- Auth gate is a single Bearer token in `Authorization` header. Token is stored only in `dashboard-server`'s `.env` and in claude.ai's Connector config.
- DNS-rebinding protection (FastMCP default) is configured to only accept the exact Funnel hostname.
- PocketBase superuser credentials never leave dashboard-server.
- If the token leaks: ssh in, regenerate it in `.env`, `sudo systemctl restart mcp_pb`, update claude.ai's connector config with the new value.

## Manual checks

```bash
# liveness
curl https://dashboard-server.tail4cfa2.ts.net:10000/health

# auth required (should 401)
curl https://dashboard-server.tail4cfa2.ts.net:10000/mcp

# MCP initialize handshake (replace TOKEN)
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  https://dashboard-server.tail4cfa2.ts.net:10000/mcp \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"manual","version":"1"}},"id":1}'
```
