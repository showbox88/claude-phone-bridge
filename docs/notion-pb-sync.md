# Notion ↔ PocketBase Sync — Architecture & Operations

This document describes the bi-directional sync between **PocketBase** (Phone Bridge's authoritative data store) and **Notion** (the user's editing surface). Built over three rolling PRs in June 2026.

---

## TL;DR

| | |
|---|---|
| **What** | Daily background sync between 8 PB collections and their Notion mirror databases. |
| **Direction** | Bi-directional. Single-side changes auto-flow; both-side changes freeze and ask. |
| **Trigger** | systemd timer every hour; Python guard runs the actual sync only at the configured local hour (default 03:00 America/New_York). |
| **User UI** | Notion's "Sync Activity" database = the pending-decision queue. Phone Bridge auto-creates a chat session when there's something to decide. |
| **Authority** | PB is the system of record. Notion is the editing surface. Neither is "the master" — conflicts are user-resolved. |
| **Scope** | trips · days · **stops** · plans · todos · contacts · locations · **journal** (8 of PB's 28 collections; stops + journal added 2026-06-03 via the stops redesign — see [docs/data-model.md](data-model.md) for the canonical schema). |

---

## Why both PB and Notion?

- **PB** is the data API Claude reads/writes. Backed by SQLite on the VM, sub-millisecond reads, schema-typed fields, transactions. Where Phone Bridge does the work.
- **Notion** is the human surface. Mobile-friendly views, drag-and-drop, easy edits from anywhere. Where the user *thinks*.

Neither is good at the other's job. The sync gives Phone Bridge a place for structured data + integrations *and* the user a place to comfortably review and edit.

---

## Architecture

```
                ┌──────────────────────────────────────────────────────┐
                │  dashboard-server   (Tailscale, US-East)             │
                │                                                       │
                │  ┌────────────┐                                       │
                │  │ PocketBase │ ← phone-bridge writes Smart Note data │
                │  │ :8090      │ ← Claude SDK reads via mcp__pb__*     │
                │  └─────┬──────┘                                       │
                │        │                                              │
                │  ┌─────┴──────────────────────────────────────────┐   │
                │  │  notion_sync.runner   (oneshot, 03:00 ET)      │   │
                │  │                                                │   │
                │  │  Phase 0: apply_pending_decisions              │   │
                │  │           ↳ reads Sync Activity, writes both   │   │
                │  │             sides per decision                 │   │
                │  │  Phase 1: categorize + dispatch                │   │
                │  │           ↳ single-side → silent sync          │   │
                │  │           ↳ both-side  → write Conflict + skip │   │
                │  │           ↳ vanished   → write Delete? + skip  │   │
                │  │  Phase 2: notify (chat session)                │   │
                │  │  Phase 3: cleanup 90-day-old resolved rows     │   │
                │  └─────┬──────────────────────────────────────────┘   │
                │        │                                              │
                │  ┌─────┴────────────┐  ┌──────────────────────┐       │
                │  │ phone-bridge     │  │ systemd timer        │       │
                │  │ FastAPI :8001    │  │ notion-sync.timer    │       │
                │  │ mcp__pb__sync_*  │  │ (hourly check)       │       │
                │  └──────────────────┘  └──────────────────────┘       │
                └──────────────────────┬───────────────────────────────┘
                                       │ HTTPS  (rate limit 2 req/s)
                                       ▼
                            ┌──────────────────────────┐
                            │  Notion API              │
                            │  6 sync target DBs       │
                            │  + Sync Activity DB      │
                            └──────────────────────────┘
```

---

## Repository layout

```
phone-bridge/
├── notion_sync/                ← the sync package (PR1+PR2+PR3)
│   ├── __init__.py             ← module marker, version notes
│   ├── pb_api.py               ← PB REST client (auth + CRUD, paginated)
│   ├── notion_api.py           ← Notion REST client, 2 req/s throttle
│   ├── codec.py                ← PB ↔ Notion field-value conversion
│   ├── matching.py             ← fuzzy title+date matcher (initial align)
│   ├── transform.py            ← row-level: notion_page ↔ pb_record (PR2)
│   ├── changeset.py            ← pure categorizer: NoChange / *Change / *New / *Vanished
│   ├── backup.py               ← PB JSON snapshot to .bridge_data/backups/
│   ├── activity.py             ← Sync Activity DB read/write helpers
│   ├── logger.py               ← .bridge_data/sync.log JSON-line writer
│   └── runner.py               ← main entry point (cron + --force-now)
│
├── scripts/                    ← one-shot operational scripts
│   ├── __init__.py
│   ├── setup_notion_sync_db.py ← PR1 bootstrap: add pipeline cols + create Sync Activity
│   └── reconcile_initial.py    ← PR1 one-shot data alignment
│
├── deploy/                     ← systemd units (PR2)
│   ├── notion-sync.service     ← oneshot, drops to dev user
│   ├── notion-sync.timer       ← OnCalendar=hourly
│   └── install_systemd.sh      ← idempotent installer
│
├── tests/notion_sync/          ← 58 tests
│   ├── test_codec.py           ← 22
│   ├── test_matching.py        ← 12
│   ├── test_backup.py          ← 2
│   ├── test_changeset.py       ← 10
│   └── test_runner_guard.py    ← 12  (zoneinfo + paused + bad-tz)
│
├── pocketbase/pb_migrations/
│   ├── 1779465616_create_sync_meta.js          ← sync_config + sync_global
│   └── 1779465617_add_sync_pipeline_fields.js  ← notion_id / notion_last_edited / last_synced_at on 6 sync targets
│
├── pb_tools.py                 ← in-process MCP server (pb_* + sync_*)
├── push.py                     ← Web Push (unused for sync now; kept for other paths)
├── db.py                       ← Phone Bridge's SQLite (sessions/messages)
├── server.py                   ← FastAPI app, wires phone-bridge
│
├── docs/
│   ├── notion-pb-sync.md       ← this file
│   └── superpowers/
│       ├── specs/2026-06-02-notion-pb-sync-design.md
│       └── plans/
│           ├── 2026-06-02-notion-pb-sync-pr1-foundation.md
│           └── 2026-06-03-notion-pb-sync-pr2-daily-runner.md
│
└── CLAUDE.md                   ← operational notes (see Notion sync section)
```

---

## The data model

### 1. Sync-target collections

These 6 PB collections sync bi-directionally with their Notion counterparts:

| PB | Notion DB | Title field | Date field (for matching) |
|---|---|---|---|
| `trips`     | Trips    | `title` | `date_start` |
| `days`      | Day      | `name`  | `date`       |
| `plans`     | Plans    | `title` | `target_date`|
| `todos`     | Todos    | `title` | `due_date`   |
| `contacts`  | Contacts | `name`  | (none)       |
| `locations` | Location | `name`  | (none)       |

The other 22 PB collections (claude_memos, daily_briefing, transactions, etc.) are NOT synced. They live PB-side only.

### 2. Pipeline fields (added to every sync target)

**PB side:**
- `notion_id` (text) — the linked Notion page UUID.
- `notion_last_edited` (date) — the `last_edited_time` we saw on Notion at our last successful sync. Used for change detection.
- `last_synced_at` (date) — wall-clock of our last sync. For audit only.

**Notion side:**
- `pb_id` (rich_text) — the linked PB record ID.
- `last_synced_at` (date) — same purpose as PB side.

Both sides get a unique index / "hidden in view" treatment so the user doesn't see them in normal browsing.

### 3. Sync config (PB collection)

`sync_config` — one row per enabled sync target. Fields:
- `collection` (text, unique) — name of the PB collection.
- `notion_db_id` (text) — the corresponding Notion DB UUID.
- `enabled` (bool) — global on/off for this target.
- `field_map_overrides` (json) — when Notion's column name doesn't snake-case cleanly to the PB field. Default `{}`.
- `last_synced_at` (date) — runner updates after each pass; used as the change-detection cutoff next run.
- `last_sync_summary` (text) — human-readable last-pass result.

`sync_global` — single row of cross-collection settings:
- `timezone` (text, default `America/New_York`)
- `sync_hour_local` (number, default `3`)
- `paused` (bool)
- `last_run_at` (date)

### 4. Sync Activity (Notion DB)

The user's queue. Lives in Notion (not PB) so the user can browse it natively and click row links to the actual records.

| Property         | Type        | What it holds |
|---|---|---|
| `title`          | title       | "{op} · {summary[:60]}" |
| `op`             | select      | `Conflict` / `Delete?` / `Possible duplicate` / `Schema mismatch` / `Auto-applied`* |
| `direction`      | select      | `Notion→PB` / `PB→Notion` / `Both` / `None` |
| `collection`     | select      | which sync target |
| `record_link`    | url         | direct Notion link to the affected record (when one exists) |
| `pb_id`          | rich_text   | PB record id (when relevant) |
| `notion_id`      | rich_text   | Notion page id (when relevant) |
| `summary`        | rich_text   | one-line diff or label |
| `pb_snapshot`    | rich_text   | JSON of the PB row at detection (used by the applier) |
| `notion_snapshot`| rich_text   | JSON of the Notion page (after `notion_page_to_pb_dict`) |
| `decision`       | select      | `Pending` → user picks → `Use Notion` / `Use PB` / `Delete both` / `Keep both` / `Merge` |
| `detected_at`    | date        | when runner first noticed |
| `applied_at`     | date        | when the applier executed the decision (null = not yet) |
| `notes`          | rich_text   | user-editable scratch field |

*`Auto-applied` was originally planned but removed: per the user's preference, silent syncs no longer write audit rows. Sync Activity holds *only* things needing user attention.

---

## The flow

### Single-side change (the 95% path)

```
PB:    [foo] modified at t1
Notion: [foo] last_edited_time = pb.notion_last_edited (unchanged)
sync_config.last_synced_at = t0 < t1

→ runner sees pb_changed=True, notion_changed=False
→ PbOnlyChange action
→ _apply_pb_to_notion: nc.update_page() with PB's new data
→ PB.notion_last_edited refreshed from the response
→ Sync Activity: untouched
→ User: no notification, no decision needed
```

Symmetric for Notion-only changes. The result: when only one side moved, the other catches up silently.

### Both-side change (the safety path)

```
PB:    [foo] modified at t1   (user added "[PB改]")
Notion: [foo] modified at t2  (user added "[notion改]")
sync_config.last_synced_at = t0 < min(t1, t2)

→ runner sees pb_changed=True, notion_changed=True
→ BothChanged action
→ write_conflict(...) → Sync Activity gets a new row:
     op=Conflict, decision=Pending, pb_snapshot=..., notion_snapshot=...
→ Neither side is written
→ Phase 2 notify creates Phone Bridge chat session
       "📋 同步待确认 1 项"

Next run, AND every future run:
→ frozen_pairs_for_collection() sees the Pending row
→ Returns (pb_ids={foo.pb_id}, notion_ids={foo.notion_id})
→ Every action involving this row is skipped
→ pb and notion data on this row are FROZEN until user decides

User opens Sync Activity, sets decision = "Use Notion" (say):
→ Sync Activity row now has decision="Use Notion", applied_at=null
→ Next runner pass, apply_pending_decisions() picks it up:
     _apply_one_decision("Use Notion")
     ↳ pb.update_record(notion_snapshot fields) — title becomes "[notion改]"
     ↳ pb.notion_last_edited refreshed
     ↳ Sync Activity row marked applied_at=today
→ Next freeze check: the row is no longer Pending → unfrozen
→ Normal sync resumes on it
```

### Deletion (Notion archives a page, or PB row deleted)

Detection: when a linked PB row's `notion_id` no longer resolves in the Notion query result (`NotionVanished`), or vice versa (`PbVanished`).

Behavior identical to BothChanged: enqueue `op=Delete?` to Sync Activity, freeze, user decides (`Use Notion` = revive on Notion side meaningless → use `Delete both` or `Keep both` instead).

### New row

PB row with empty `notion_id` → `PbNew` → create Notion page, link back. Silent.
Notion page with empty `pb_id` → `NotionNew` → create PB record, link back. Silent.

(The `reconcile_initial.py` script does the bulk version of this for first-time alignment, plus fuzzy match for already-similar rows.)

---

## The categorizer (`changeset.py`)

A pure function — no I/O, no globals — that classifies each row into exactly one `Action` dataclass:

| Action          | When                                                    | Dispatch                |
|---|---|---|
| `NoChange`      | linked + neither side changed since last_synced_at      | skip                    |
| `PbOnlyChange`  | PB changed, Notion didn't                               | apply PB→Notion silently|
| `NotionOnlyChange`| Notion changed, PB didn't                             | apply Notion→PB silently|
| `BothChanged`   | both changed                                            | enqueue Conflict, freeze|
| `PbNew`         | PB row has no notion_id                                 | create in Notion silently|
| `NotionNew`     | Notion page has no pb_id                                | create in PB silently   |
| `NotionVanished`| PB has notion_id → page missing from Notion fetch       | enqueue Delete?, freeze |
| `PbVanished`    | Notion page has pb_id → PB row missing                  | enqueue Delete?, freeze |

Why pure: it's testable in isolation (10 tests cover every branch + tricky timestamp edges). The runner.py dispatcher does all I/O.

---

## The runner (`runner.py`)

The main entry point. Sequence per `main()`:

1. **Time guard.** Read `sync_global`. If `paused=True` → log `skipped_paused`, exit. Else compute current hour in `timezone`; if it's not `sync_hour_local` → exit silently (this happens 23×/day with no side effects). `--force-now` bypasses this.
2. **For each enabled `sync_config` row** (a "collection"):
   1. Read PB collection schema (`field_types`) and Notion DB schema (for type-aware codec).
   2. **Phase 0** — `apply_pending_decisions()`: scan Sync Activity for `decision != Pending AND applied_at is empty`. Apply each (Use Notion / Use PB / Delete both / Keep both). Mark `applied_at`.
   3. **Freeze set** — `frozen_pairs_for_collection()`: query Sync Activity for `decision=Pending AND applied_at empty`, return (frozen_pb_ids, frozen_notion_ids).
   4. **Categorize** — `changeset.categorize(pb_rows, notion_rows, last_synced_at)`.
   5. **Dispatch** — for each Action:
      - If `pb_id ∈ frozen_pb_ids OR notion_id ∈ frozen_notion_ids` → skip, count as `frozen_skipped`.
      - Else dispatch by Action class:
        - `*Change`/`*New` → silent sync, no Sync Activity write.
        - `BothChanged` → `write_conflict()` (first-time only; freeze prevents re-fire).
        - `*Vanished` → `write_delete_question()`.
   6. Update `sync_config.last_synced_at`, `last_sync_summary`.
3. Update `sync_global.last_run_at`.
4. **Notify** — `notify_pending()`: if Pending count > 0 AND (last alert > 6h ago OR pending set changed), create a Phone Bridge chat session "📋 同步待确认 N 项" with markdown summary.
5. **Cleanup** — `cleanup_resolved_activity(days=90)`: archive Sync Activity rows whose `applied_at < today - 90`.

All step boundaries log JSON events to `.bridge_data/sync.log` for forensics.

---

## MCP tools (Claude can call these in Phone Bridge chat)

All four are SAFE-listed → no permission prompt.

| Tool                              | What it does                                                    |
|---|---|
| `mcp__pb__sync_now [collection?]` | Spawn `notion_sync.runner --force-now [--only X]`. Returns exit code, last 12 sync.log lines. |
| `mcp__pb__sync_queue_status`      | Query Sync Activity Pending; return count + first 10 (op, collection, summary, link). |
| `mcp__pb__sync_pause`             | Set `sync_global.paused = true`. Hourly cron exits silently. |
| `mcp__pb__sync_resume`            | Set `sync_global.paused = false`. |

Example: "Claude, 同步队列有什么?" → calls `sync_queue_status` → "目前没有 Pending 项" or "有 2 项,分别是…".

---

## User notification UX

Pattern stolen from `report.py` (weekly report). When the runner finishes and finds Pending items:

1. Hash the sorted Pending row IDs.
2. Read `.bridge_data/sync_alert_state.json`. If hash matches AND last alert < 6h ago → skip (don't spam).
3. Else `db.create_session(title="📋 同步待确认 N 项", mode="chat", model="")`.
4. `db.append_message(sid, "assistant_text", {"text": markdown})` with a grouped list:
   ```
   ## 📋 同步待确认 3 项

   ### 🔀 冲突 — 1 项
   - **todos** · 去中国超市买大米 [PB改]
     - [打开 Sync Activity 那一行](https://www.notion.so/...)

   ### 🗑️ 删除? — 2 项
   - **todos** · Notion page missing: ...
   - **todos** · PB record missing: 测试

   ---

   怎么处理: 打开 Sync Activity DB,把每行的 `Decision` 改成
   `Use Notion` / `Use PB` / `Delete both` / `Keep both`。下一次同步
   (每天 03:00 ET,或叫 Claude `同步一下`)会自动执行你的选择。
   ```
5. Save new state. User opens Phone Bridge → sees the session at the top of the sidebar.

No VAPID, no OS notifications, no install steps. Sessions persist across devices since they live in the bridge.db SQLite.

---

## Limitations / known holes

These are real but accepted for the MVP:

- **Relation fields don't sync.** PB stores PB record IDs, Notion stores Notion page UUIDs — the ID spaces don't overlap. `transform.py` skips relation fields in both directions. PB relations stay at whatever `reconcile_initial.py` set them to; user edits to relations don't propagate.
- **Merge decision.** UI option exists in the spec but `_apply_one_decision` raises on it. Future PR can split snapshots field-by-field and prompt.
- **File / editor fields.** Long content (>1900 chars) gets truncated in `pb_snapshot`/`notion_snapshot` rich_text. JSON might be malformed on parse → applier logs `decision_apply_error` and leaves the row Pending.
- **Schema drift.** New columns added to PB or Notion are silently dropped on the side that doesn't have them. No `Schema mismatch` entry is emitted automatically.
- **PB autodate vs Python `now_iso_datetime()`.** Both produce `YYYY-MM-DD HH:MM:SS.SSSZ` now. Comparing across these as lex strings works *because* of the format alignment. Don't break that.
- **Race window.** The runner is not transactional across sides. If it crashes between writing one side and updating tracking fields, the next run will re-detect and either redo (idempotent on linked-ID writes) or stage a conflict (false positive). Acceptable for a daily cron; PR3+ could add a transactional log.

---

## Operations runbook

### Deploy

From local Windows:
```powershell
deploy
```

The `deploy` tool (configured via `.deploy.json`) tars, uploads, installs deps, and `sudo systemctl restart phone-bridge`. PocketBase is a separate service — restart only when you change `pb_migrations/` (after copying them to `/opt/pocketbase/pb_migrations/`).

### Install / re-install systemd timer

```powershell
ssh dashboard-server "cd /home/dev/phone-bridge/deploy && bash install_systemd.sh"
```

Idempotent.

### Force a sync now

```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python -m notion_sync.runner --force-now"
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python -m notion_sync.runner --force-now --only trips"
```

Or in Phone Bridge chat: "同步一下" → Claude calls `mcp__pb__sync_now`.

### Pause / resume

In Phone Bridge chat: "暂停同步" / "恢复同步" → `mcp__pb__sync_pause` / `mcp__pb__sync_resume`. Or PATCH `/api/collections/sync_global/records/<id>` with `{"paused": true|false}` directly.

### Read logs

```powershell
ssh dashboard-server "tail -50 /home/dev/phone-bridge/.bridge_data/sync.log | jq ."
ssh dashboard-server "journalctl -u notion-sync.service -n 50 --no-pager"
```

### Re-run initial reconcile (rare)

If you added a brand new collection to `sync_config` or did something destructive in Notion:
```powershell
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python scripts/reconcile_initial.py --only <collection> --dry-run"
ssh dashboard-server "cd /home/dev/phone-bridge && set -a && . ./.env && set +a && .venv/bin/python scripts/reconcile_initial.py --only <collection>"
```

Backs up PB to `.bridge_data/backups/<ts>/` before any write.

### Diagnose

| Symptom | Likely cause | Where to look |
|---|---|---|
| `applied=N` but data didn't move | apply_error in log | grep `apply_error` in sync.log |
| Same row keeps showing up as a different category | format mismatch in timestamps | check `now_iso_datetime` output vs `pb.updated` shape |
| Push session never appears | `BRIDGE_DATA_DIR` not pointing at the bridge.db dir | check `db.init` path in runner.py |
| User decision not applying | freeze still sees it | check `applied_at` is set on Sync Activity row |
| Cron silently doing nothing | `sync_global.paused = true` | `cat /api/collections/sync_global/records` |
| Runs at wrong hour | `sync_global.timezone` stale | PATCH `sync_global` with correct tz; takes effect next hourly tick |

---

## Evolution / how we got here

| Phase | Commits | Outcome |
|---|---|---|
| **PR1** — Foundation + initial alignment | 15 commits, `0045087..bb7d929` | Schema in place, ~80%-overlapping data aligned via fuzzy match. Sync Activity gained `Possible duplicate` entries for ambiguous cases. |
| **PR2** — Daily cron runner | 15 commits, `7bab3b9..1b9eadf` | Systemd hourly + timezone-guard. Implemented after live testing exposed: ms-precision timestamp mismatch (perpetual flap), relation-field ID-space mismatch (validation errors), data-loss on conflict (`pb.notion_last_edited` stale), cascade on decision-applied (freeze didn't include applied-but-not-applied). All four fixed before commit `ae76e2a` (the freeze approach). |
| **PR3** — Decision applier + alerts + MCP + cleanup | 7 commits, `6c40f19..cb73d84` | apply_pending_decisions for all 4 user decisions. 4 SAFE MCP tools. Replaced VAPID push with weekly-report-style chat-session alert (better UX, zero config). 90-day archive of resolved Sync Activity rows. |

Total: ~37 commits, 58 passing tests, ~3000 lines including tests + docs.

---

## Adding a new sync target

Since 2026-06-04, registering a new sync target is a 2-step user flow,
not a code change:

1. **Create the PB collection** via the Phone Bridge chat:

   > "Make a PB collection called `<name>` with fields …"

   Claude calls `pb_create_collection`. PB writes a JS migration file
   that the next deploy will pull back into git.

2. **Register it for sync** via Phone Bridge → 同步设置 → "+ 新增同步表".
   Pick the new collection, set `title_field`, optionally `date_field`,
   leave `auto_sync` on (default). Click "创建并同步".

The server auto-provisions a Notion DB matching the PB schema, inserts
a `sync_config` row, adds the collection name to Sync Activity's select,
and runs `reconcile_initial --only <new>` in the background.

See [`docs/sync-registry-design.md`](sync-registry-design.md) for the
mechanism — REST endpoints, PB→Notion type mapping table, relation
handling, and out-of-scope notes.

---

## What's NOT in scope (future)

- **Relation field translation** — needs an ID-mapping layer (PB ↔ Notion) for cross-side relation writes.
- **Merge decision** — needs field-level snapshot diffing.
- **Realtime sync via Notion webhooks** — Notion only offers webhooks on enterprise plans; we chose daily cron deliberately.
- **Schema drift auto-detection** — currently silent. Could emit `Schema mismatch` Sync Activity rows.
- **Multi-Notion-workspace** — single workspace assumed.
- **Auto-resubscribe / VAPID setup wizard** — push.py exists but isn't used for sync alerts. Could be revived as an alternative notification channel.
