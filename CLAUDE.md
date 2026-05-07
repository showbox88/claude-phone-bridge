# Claude Code instructions for Phone Bridge

Phone-friendly PWA that drives `claude-agent-sdk` so you can run Claude Code
from any phone or laptop. Deployed on `dashboard-server` (192.168.1.168 on LAN,
`100.81.67.15` on the tailnet) at `https://dashboard-server.tail4cfa2.ts.net/`.

Listens on `127.0.0.1:8001` inside the VM. Tailscale Serve reverse-proxies
HTTPS in front of it. Authentication is implicit: only devices logged into the
user's Tailscale account can reach the URL.

## Deploy

```powershell
deploy
```

`.deploy.json` is configured. The shared `deploy` tool:
1. Tars the project (excluding `.venv`, `.bridge_uploads`, `.bridge_data`, `.env`)
2. Uploads to `/home/dev/phone-bridge`
3. Recreates `.venv` if missing, runs `pip install -r requirements.txt`
4. `sudo systemctl restart phone-bridge`
5. Hits `https://dashboard-server.tail4cfa2.ts.net/api/health`

`.bridge_uploads` and `.bridge_data` (uploaded files + sessions) are listed in
both `exclude` and `keep_files` — they live on the VM only and survive deploys.

## First-time auth (one-time, manual)

The `claude-agent-sdk` package bundles a Claude binary that needs OAuth login
once. After first deploy:

```powershell
ssh dashboard-server
cd /home/dev/phone-bridge
.venv/bin/python -c "from claude_agent_sdk import ...; ..."   # adjust to package's login flow
```

Or set `ANTHROPIC_API_KEY` in `/home/dev/phone-bridge/.env` to skip OAuth.

## Defaults

| Var | Value |
|---|---|
| HOST | `127.0.0.1` |
| PORT | `8001` |
| DEFAULT_CWD | `/home/dev` (so Claude can navigate to any project) |
| ALLOWED_ORIGINS | `*` (Tailscale is the auth boundary) |

## Logs

```powershell
ssh dashboard-server 'sudo journalctl -u phone-bridge -f'
ssh dashboard-server 'systemctl status phone-bridge'
```

## When NOT to deploy

- Don't deploy while you're in an active Phone Bridge chat — it'll restart
  the service and drop your WebSocket. Sessions resume from disk so
  conversation isn't lost, but the in-flight tool call may abort.
- Don't change `DEFAULT_CWD` to a path Claude shouldn't have access to —
  Claude can spawn shell commands within `cwd` and below.

## Architecture

```
Phone / laptop on tailnet
   ↓ HTTPS
Tailscale Serve  (dashboard-server.tail4cfa2.ts.net)
   ↓ reverse proxy
phone-bridge.service  (FastAPI on 127.0.0.1:8001)
   ↓ spawns
claude-agent-sdk subprocess
   ↓ reads/writes
/home/dev/<project>/  (tickt-traker, dashboard, …)
```
