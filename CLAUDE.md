# Claude Code instructions for Phone Bridge

Phone-friendly PWA that drives `claude-agent-sdk` so you can run Claude Code
from any phone or laptop. Deployed on `dashboard-server` (192.168.1.168 on LAN,
`100.81.67.15` on the tailnet) at `https://dashboard-server.tail4cfa2.ts.net/`.

Listens on `127.0.0.1:8001` inside the VM. Tailscale Serve reverse-proxies
HTTPS in front of it. Authentication: the public surface returns a generic `503` decoy to everything
unauthenticated — no login page, no hint the service exists. The only login
door is a secret **super link** (a high-entropy URL) that gates a password +
TOTP form; passing it enrols the device (90-day sliding cookie). Manage/rotate
the link over SSH: `.venv/bin/python -m app.auth.cli rotate-link`. See
[docs/operations/superlink-runbook.md](docs/operations/superlink-runbook.md).

## Refactor history (completed 2026-06-10)

The 2026-06-06 → 2026-06-10 refactor roadmap is **done**. 8 phases merged
to main covering: guardrails / foundation / PB client / app/ package split /
session multi-instance / frontend ES modules / sync race fix + perf / tests
+ structlog. Phase 6b (CSRF / SameSite Strict / Origin) and 6c (OTel /
request_id) were evaluated and explicitly skipped — see
[docs/superpowers/specs/2026-06-06-refactor-roadmap.md](docs/superpowers/specs/2026-06-06-refactor-roadmap.md)
"路线图收尾" section for the reasoning.

### Working rules going forward

- **Branch naming:** new features on `feature/<slug>`, bug fixes on
  `fix/<slug>`, hotfixes on `hotfix/<slug>`. Phase branches are retired.
- **Commit prefixes** follow conventional commits: `feat:` / `fix:` /
  `refactor:` / `docs:` / `chore:` / `test:` — no longer restricted to
  `refactor:` / `docs:` only on main.
- **Before merging to main:**
  - Run `pytest tests/ --ignore=tests/test_safe_filename.py` — must be all green
  - Run smoke if the change touches backend routes/WS:
    ```bash
    BASE=https://dashboard-server.tail4cfa2.ts.net \
      BRIDGE_COOKIE='bridge_session=...' \
      python tests/smoke_backend.py
    ```
    Must print `OK: all smoke checks passed`
- **Auth code is data-safety critical** — `app/auth/*`, `auth.py`, the
  super-link gate flow, the 503 decoy, and the 90-day cookie. Any change
  here needs explicit user discussion + manual gate POST verification
  before merge.
- **Rollback procedure:** [docs/operations/rollback.md](docs/operations/rollback.md).
  Anyone touching `main` should have skimmed it.

## Deploy

```powershell
deploy
```

`.deploy.json` is configured. The shared `deploy` tool:
1. Tars the project (excluding `.venv`, `.bridge_uploads`, `.bridge_data`, `.env`)
2. Uploads to `/home/dev/phone-bridge`
3. Recreates `.venv` if missing, runs `pip install -r requirements.txt`
   (the lockfile — to upgrade a dep, edit `requirements.in` then
   `python -m piptools compile --output-file requirements.txt --strip-extras requirements.in`)
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

### Expenses redesign (2026-06-05)

`transactions` was reshaped into `expenses` — a child table of
`stops`/`days`/`trips`. A stop can hold N expenses (公园 visit = 门票 +
冰淇淋 + 水); daily expenses w/o a trip just hang off the day with
`expense.stop=empty`, `expense.day=今天`. `days.trip` is now optional
(为日常容器). All 11 old transactions and 6 money-bearing stops were
migrated; 4 "代付 Monica" rows auto-classified to `expense_category=代付`.

The runtime expects writer-side to: auto-fill `amount_usd`
(= amount for USD, = amount × rate otherwise), store refunds as negative
amount, and keep `expense.trip = expense.day.trip` in sync (denormalized).

See **[docs/data-model.md §2.9](docs/data-model.md)** for the schema and
**[docs/superpowers/specs/2026-06-05-expenses-redesign-design.md](docs/superpowers/specs/2026-06-05-expenses-redesign-design.md)**
for the design rationale. Sync wiring (add expenses to sync_config via
the 同步设置 UI) is a separate follow-up.

### Timezone-aware data (shipped 2026-06-05)

All trip-stack collections now carry `timezone` (IANA name) — `locations`,
`stops`, `days`, `expenses`, `foods`. `todos` carries `due_at` (UTC) +
`due_tz` for cross-tz reminders. See
**[docs/superpowers/specs/2026-06-05-timezone-design.md](docs/superpowers/specs/2026-06-05-timezone-design.md)**
for the design and `tz_resolver.py` for the shared helpers.

Frontend reports `client_tz` per WS message; server stashes on
`state.client_tz` and injects it into the agent system prompt. Agents follow
the fallback chain documented in `mcp_pb/SMARTNOTE_PROMPT.md` (Timezone
section).

### Sync registry (where the list of synced tables lives)

As of 2026-06-04 the per-target sync configuration lives entirely in the
PB `sync_config` table — three new columns (`title_field`, `date_field`,
`auto_sync`) replace what used to be hardcoded Python dicts in three
files. To add a new sync target:

1. Create the PB collection (chat with Claude → `pb_create_collection`)
2. Open Phone Bridge → 同步设置 → click **+ 新增同步表** → pick the new
   collection, set title_field / auto_sync, hit "创建并同步"
3. The server, in one POST, does ALL of:
   - Adds 5 columns + a unique index to the PB collection (idempotent):
     - **Pipeline:** `notion_id (text 100)`, `notion_last_edited (date)`, `last_synced_at (date)`
     - **Autodate (system):** `created (autodate onCreate)`, `updated (autodate onCreate+onUpdate)`
     - **Index:** `UNIQUE INDEX idx_<name>_notion_id ON <name>(notion_id) WHERE notion_id != ''`
   - Creates the matching Notion DB (columns inferred from PB schema, plus `pb_id` and `last_synced_at` pipeline columns)
   - Inserts the `sync_config` row with title_field / date_field / auto_sync
   - Adds the collection name to Sync Activity's `collection` select
   - Spawns `reconcile_initial --only <new>` in the background

The pipeline + autodate field auto-add is **critical**: `pb_create_collection`
(MCP tool used by chat) does NOT add `created`/`updated` autodate the way PB
admin UI does, and without `updated` the runner's `categorize()` can never
detect PB-side changes (every row is forever `NoChange`). The provisioner
fills this gap so collections registered via chat-then-UI work the same as
collections built through migrations.

**Adding new PB select options:** if you add a value to a PB select
field (e.g. add "电子" to a `category`), Notion's matching column does
NOT have to be updated manually — Notion's API auto-creates missing
select options when the runner writes a page with a new value. So
the flow "add PB select option → change a row → next sync" just works.

No code changes required for new sync targets. See
[`docs/sync-registry-design.md`](docs/sync-registry-design.md) for the
field-by-field design, the PB→Notion type mapping table, relation
handling rules, and the REST API reference.

**Disaster-recovery snapshot:** the runtime state of the registry is
mirrored to `notion_sync/registry.snapshot.yaml`. It's NOT auto-updated
— after any UI change you care about, run
`python scripts/dump_sync_registry.py` and commit the diff.

## Trip data model: stops redesign (shipped 2026-06-03)

`days` was split into a pure container (`days`) + atomic events (`stops`)
so a real travel day can hold many events. All five phases of the migration
have been executed; see
**[docs/superpowers/specs/2026-06-03-stops-redesign-design.md](docs/superpowers/specs/2026-06-03-stops-redesign-design.md)**
for the architecture, **[docs/stops-redesign-runbook.md](docs/stops-redesign-runbook.md)**
for the historical 5-phase migration runbook, and
**[docs/data-model.md](docs/data-model.md)** for the as-built schema (the
canonical reference going forward).

Key consequence: `days` no longer holds `reserved / checkin / amount /
currency / rate / amount_usd / activity_type / score / location /
actual_lat / actual_lng` — all those fields live on `stops` now. Writes
that previously went to `days` need to upsert a `days` container + create
a `stops` row underneath. See `CHECKIN.md` for the updated protocol.

## Data model reference (for sync agents)

**[docs/data-model.md](docs/data-model.md)** is the field-by-field reference
for all 8 synced PB collections + their Notion DBs, the codec rules
(snake↔Title, type envelopes), pipeline fields, Sync Activity structure,
known limitations (notably: relations are NOT bidirectionally synced today),
and common operations for agents (add a sync target, debug a stuck row,
add a category to stops, etc.). Read this before touching anything in
`notion_sync/`.

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
- User decisions on Pending rows (set in Notion) are applied automatically
  on the next runner pass via `apply_pending_decisions()`; the row's
  `applied_at` field then stamps when it ran. Supported decisions:
  `Use Notion` / `Use PB` / `Delete both` / `Keep both`.
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
