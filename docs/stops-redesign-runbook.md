# Stops redesign runbook

Operational checklist for executing the days→days+stops redesign. Pair with
[2026-06-03-stops-redesign-design.md](superpowers/specs/2026-06-03-stops-redesign-design.md)
(architecture) and CLAUDE.md (general sync ops).

The five phases below are sequenced for safety — DO NOT skip ahead. Each
phase is recoverable up to and including its commit point; once you start the
next phase, rollback gets harder.

---

## Phase 0 — Pre-flight

**Pause sync, snapshot both sides.**

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a

# 1. Pause sync — every hourly tick will now skip silently.
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
rows = pb.list_records('sync_global', sort='')
pb.update_record('sync_global', rows[0]['id'], {'paused': True})
print('sync paused')
"

# 2. Confirm next tick will skip.
sudo journalctl -u notion-sync.service --since '2 minutes ago' -n 20
# (you can also just wait for the next hourly tick and confirm 'skipped_paused')

# 3. PB backup. This is on top of the per-script backup that Phase 2 takes.
.venv/bin/python -c "
from pathlib import Path
from notion_sync.pb_api import PBClient
from notion_sync.backup import backup_collections
print(backup_collections(PBClient(), Path('.bridge_data/backups')))
"
```

Notion has no backup API — accept that asymmetry. If you've made critical
manual edits in Notion DBs recently, export them to CSV from Notion UI now.

---

## Phase 1 — Apply additive PB migrations

Three migrations are already in the repo (commit f556fb4):
- `1779465618_create_stops.js`
- `1779465619_extend_days_for_stops_migration.js`
- `1779465620_extend_journal_for_stops.js`

```bash
# From your local Windows shell:
deploy
```

After deploy, verify on the server:

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a

.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
cols = {c['name'] for c in pb.list_collections()}
assert 'stops' in cols, 'stops collection missing'
days_fields = {f['name'] for c in pb.list_collections() if c['name'] == 'days' for f in c['fields']}
assert 'weather' in days_fields and 'migrated_to_stop_id' in days_fields
journal_fields = {f['name'] for c in pb.list_collections() if c['name'] == 'journal' for f in c['fields']}
assert 'related_stop' in journal_fields and 'notion_id' in journal_fields
print('Phase 1 OK')
"
```

At this point sync can technically resume safely (no destructive changes
yet) — but leave it paused so the data migration runs against a stable
snapshot.

---

## Phase 2 — Data migration (days rows → stops rows)

`scripts/migrate_days_to_stops.py` reads every legacy `days` row and:
- creates one `stops` row carrying the moved fields
- assigns `stops.day` to the canonical day (first row per `(trip, date)`)
- writes `days.migrated_to_stop_id = <new stop id>` so reruns skip
- records 2nd+ days on the same `(trip, date)` to `migrate_duplicates.csv`

### 2.1 — Dry run first

```bash
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python scripts/migrate_days_to_stops.py --dry-run | tee /tmp/migrate-dry.log
```

Read the log. Sanity-check:
- Are the categories mappings reasonable for your data?
- Are the duplicate counts plausible?

### 2.2 — Real run (creates stops, no deletes yet)

```bash
.venv/bin/python scripts/migrate_days_to_stops.py | tee /tmp/migrate-run.log
```

Verify:
- "Backup written: ..." line at the top
- `stops_created` count ≈ count of legacy `days` rows
- `migrate_duplicates.csv` exists if there are any duplicates

### 2.3 — Review duplicates, then delete

Pull the CSV down or read it on the server:

```bash
cat migrate_duplicates.csv
```

Spot-check rows: does it make sense that these specific day rows are
duplicates (same trip + same date)? If yes, run the cleanup:

```bash
.venv/bin/python scripts/migrate_days_to_stops.py --apply-delete
```

(`--apply-delete` is idempotent: stops are already created, so this just
deletes the duplicate day rows. Already-migrated rows are skipped by
`migrated_to_stop_id`.)

---

## Phase 3 — Drop legacy days fields

Migration `1779465621_drop_legacy_days_fields.js` is in the repo. **It
refuses to apply if any legacy day row hasn't been migrated** (safety check
inside the migration). So if Phase 2 wasn't run on the prod DB, the deploy
fails loud — not silent data loss.

```bash
# Local Windows shell:
deploy
```

Verify:

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
days_fields = {f['name'] for c in pb.list_collections() if c['name'] == 'days' for f in c['fields']}
for gone in ('reserved','checkin','amount','currency','rate','amount_usd',
             'activity_type','score','location','actual_lat','actual_lng',
             'migrated_to_stop_id'):
    assert gone not in days_fields, f'still present: {gone}'
print('Phase 3 OK')
"
```

---

## Phase 4 — Notion-side manual setup + sync_config

### 4.1 — Create Notion DBs by hand

In Notion (the same workspace as your existing Trips / Day / etc. DBs),
create two databases:

**Stops** — properties (match titles exactly; codec snake-cases on the fly):

| Property | Type | Notes |
|---|---|---|
| Name | Title | required |
| Date | Date | |
| Reserved | Date | include time |
| Checkin | Date | include time |
| Categories | Multi-select | options: `打卡, 酒店, 餐厅, 购物, 体验, 交通, 笔记, 消费` |
| Amount | Number | |
| Currency | Select | options: `JPY, EUR, USD, CNY, 其他` |
| Rate | Number | |
| Amount Usd | Number | |
| Note | Text | |
| Actual Lat | Number | |
| Actual Lng | Number | |
| Pb Id | Text | hidden in views |
| Last Synced At | Date | hidden in views |

**Do NOT add Notion relation columns this round** — relations are PB-only
(see spec §5.3). Add them later when relation-sync PR ships.

**Journal** — properties:

| Property | Type | Notes |
|---|---|---|
| Title | Title | required |
| Date | Date | |
| Mood | Select | options: `Happy, Sad, Anxious, Excited, Calm, Frustrated, Grateful, Reflective, Energized` |
| Type | Select | options: `Learning, Feeling, Observation, Event, Diary, Reminder` |
| Tags | Multi-select | options: `工作, 家人, 学习, 读书, 生活` |
| Content | Text | (Notion `rich_text` — long-form) |
| Pb Id | Text | hidden |
| Last Synced At | Date | hidden |

### 4.2 — Capture the new DB IDs and add sync_config rows

From each DB's "Copy link to view" URL, grab the 32-char ID (the segment
after the last `/` and before `?v=`). Then:

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a

.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
pb.create_record('sync_config', {
    'collection': 'stops',
    'notion_db_id': 'PASTE_STOPS_DB_ID_HERE',
    'enabled': True,
    'field_map_overrides': {},
})
pb.create_record('sync_config', {
    'collection': 'journal',
    'notion_db_id': 'PASTE_JOURNAL_DB_ID_HERE',
    'enabled': True,
    'field_map_overrides': {},
})
print('sync_config rows added')
"
```

### 4.3 — Initial reconcile (push PB stops + journal to Notion)

```bash
.venv/bin/python scripts/reconcile_initial.py --only stops
.venv/bin/python scripts/reconcile_initial.py --only journal
```

This populates each new Notion DB with rows from PB, writing the matching
`pb_id` (Notion side) and `notion_id` (PB side) so future syncs are linked.

Cross-check counts after each:
- Notion Stops DB row count == PB `stops` row count
- Notion Journal DB row count == PB `journal` row count

---

## Phase 5 — Resume sync, verify

```bash
# Unpause.
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
rows = pb.list_records('sync_global', sort='')
pb.update_record('sync_global', rows[0]['id'], {'paused': False})
print('sync unpaused')
"

# Force one pass to verify all 8 sync targets work.
.venv/bin/python -m notion_sync.runner --force-now 2>&1 | tee /tmp/first-resume.log
```

Spot-check:
- log includes per-collection pass for: trips, days, plans, todos, contacts,
  locations, stops, journal
- no `decision_apply_error` lines
- Sync Activity (in Notion) doesn't suddenly fill with Pending conflicts
- Open a few Stops Notion pages — scalar fields look correct
- Open a few Day Notion pages — name/date/weather/note still present;
  the dormant columns from the old schema may still be there but blank

### Optional Notion cleanup

In the Notion Day DB (UI), delete these dormant columns once you've
confirmed sync is healthy:
- Reserved, Checkin, Amount, Currency, Rate, Amount Usd, Activity Type,
  Score, Location, Actual Lat, Actual Lng

(They're no longer touched by sync but consume Notion screen real estate.)

---

## Rollback

| If you're stuck after... | Recovery |
|---|---|
| Phase 1 deploy | PB migrations have `down` blocks — `pb migrate down 3` reverses them. PB data unchanged. |
| Phase 2 partial run | Reruns are idempotent (skips rows with `migrated_to_stop_id`). If you need to undo, restore the `.bridge_data/backups/<ts>/days.json` and delete the new `stops` rows. |
| Phase 3 deploy | `down` block re-adds the empty fields. Data NOT restored — you'd need the Phase 0 backup. |
| Phase 4 reconcile | Delete the freshly-created Notion pages, clear the `notion_id` field on the corresponding PB rows. Run reconcile again. |
| Phase 5 sync surprises | `paused=true` immediately, investigate Sync Activity in Notion. |

For total disaster: restore PB from the Phase 0 backup folder by stopping
the `phone-bridge` service, replacing the relevant JSON files (or running a
PB restore SQL), restarting.
