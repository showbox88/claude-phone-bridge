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

## Notion sync

PR1 + PR2 + PR3 shipped: schema, daily cron runner, decision applier, MCP
tools, in-app chat-session alerts, 90-day cleanup. See
**[docs/notion-pb-sync.md](docs/notion-pb-sync.md)** for the full
architecture / data model / flow / runbook.

Quick reference (the rest of this section is the operational TL;DR — for
anything deeper, read the doc):

**Daily operation:**
- systemd timer `notion-sync.timer` fires hourly.
- The runner reads `sync_global.timezone` + `sync_hour_local` and exits
  silently unless the current hour in that timezone equals the configured
  sync hour.
- When it does run: for each enabled `sync_config` row it categorizes
  rows into changed-one-side / changed-both / new / vanished. **Single-side
  changes and new rows are synced silently** — Sync Activity is not
  touched (the data itself is the visible result).
- **Conflicts (both sides changed) and deletes (one side vanished) are
  enqueued to Sync Activity with `decision=Pending`** so you can review
  snapshots in Notion and pick a winner. Re-detected conflicts/deletes
  don't duplicate-write (idempotent via `pending_action_exists`).
- PR2 does **not** auto-apply user decisions yet — that lands in PR3. You
  can still mark decisions in Notion; PR3 will pick them up on first run.
- `sync_config[*].last_sync_summary` reflects the latest pass.

**Force a run now:**
```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -m notion_sync.runner --force-now              # all enabled
.venv/bin/python -m notion_sync.runner --force-now --only trips # one table
```

**Pause:** set `sync_global.paused = true` via PB admin or REST. The next
hourly tick logs `skipped_paused` and exits without touching anything.

**Logs:**
- operational events JSON lines: `/home/dev/phone-bridge/.bridge_data/sync.log`
  (run_start, run_end, apply_error, skipped_paused, bad_timezone)
- conflicts/deletes: NOT in the log file — go to Notion Sync Activity DB
- systemd journal: `journalctl -u notion-sync.service -f`

**Change the schedule / timezone:** update `sync_global` in PB. Takes
effect at the next hourly tick — no systemctl reload needed.

**Re-running initial reconcile** (still available):
```bash
.venv/bin/python scripts/reconcile_initial.py --only <collection> --dry-run
.venv/bin/python scripts/reconcile_initial.py --only <collection>
```

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
