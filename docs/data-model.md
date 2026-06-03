# Phone Bridge data model reference

Single source of truth for "what data lives where" — for agents working on
sync, migration, or new features that touch PocketBase or Notion.

Pair with:
- [docs/notion-pb-sync.md](notion-pb-sync.md) — sync runner architecture
- [docs/stops-redesign-runbook.md](stops-redesign-runbook.md) — current
  in-flight migration (days→days+stops)
- [docs/superpowers/specs/2026-06-03-stops-redesign-design.md](superpowers/specs/2026-06-03-stops-redesign-design.md)
  — design doc for the stops redesign

This doc reflects the model **after** the stops redesign Phase 1 ships
(migrations 18–20 deployed). Anything not yet deployed is called out
inline.

---

## 1. Top-level shape

PocketBase is the source of truth. Notion is a read/write mirror for a
chosen subset of collections. The hourly cron runner reconciles both sides.

```
                                     ┌──────────────────────────┐
                                     │  PocketBase (SQLite)     │
PB user collections (14 total)       │  /home/dev/phone-bridge/ │
                                     │  pocketbase/pb_data/     │
                                     └──────────────────────────┘
                                              ▲
                                              │
            ┌─────────────────────────────────┼──────────────────────────┐
            │ 8 SYNC targets                  │ 6 PB-only                │
            │                                 │                          │
            │ • trips                         │ • foods                  │
            │ • days        (container)       │ • daily_briefing         │
            │ • stops       (atomic event)    │ • claude_memos           │
            │ • plans                         │ • transactions           │
            │ • todos                         │ • ideas                  │
            │ • contacts                      │ • pages                  │
            │ • locations                     │                          │
            │ • journal                       │                          │
            └─────────────────────────────────┴──────────────────────────┘
                                              ▲
                                              │ hourly cron
                                              ▼
            ┌─────────────────────────────────────────────────────────────┐
            │  Notion workspace                                           │
            │                                                             │
            │  • Trips, Day, Stops, Plans, Todos, Contacts, Location,     │
            │    Journal                — bidirectional mirror            │
            │  • Sync Activity          — queue for conflicts / deletes   │
            └─────────────────────────────────────────────────────────────┘

PB meta collections (2): sync_config (per-target settings),
                         sync_global (cron timezone / paused flag).
```

---

## 2. Pipeline fields — every synced row carries these

Added to all 8 sync-target collections (migration 17 covers the original 6;
migration 18 adds them to `stops`; migration 20 adds them to `journal`).

**PB side**:

| Field | Type | Purpose |
|---|---|---|
| `notion_id` | text | The matched Notion page UUID. Unique index `WHERE notion_id != ''`. Empty for unsynced rows. |
| `notion_last_edited` | date | The Notion-side `last_edited_time` we saw on the last successful sync. Used by change detection. |
| `last_synced_at` | date | PB-side wall-clock of last sync — `YYYY-MM-DD HH:MM:SS.SSSZ`. Audit only. |

**Notion side** (every synced DB has these two columns):

| Property | Type | Purpose |
|---|---|---|
| `pb_id` | rich_text | The matched PB record id. |
| `last_synced_at` | date | Notion-side wall-clock — `YYYY-MM-DD`. |

A row is **linked** iff `pb.notion_id != ''` AND Notion page's `pb_id` ==
that PB row's id. Linking happens automatically in three places:
- `scripts/reconcile_initial.py` (initial fuzzy match by title + date)
- `runner.py::_apply_pb_new` (a new PB row creates the Notion page)
- `runner.py::_apply_notion_new` (a new Notion page creates the PB row)

---

## 3. The 8 synced collections

Field names below are PB-side (snake_case). Notion-side property names are
auto-derived by `snake_to_title()` in `codec.py:36` unless overridden via
`sync_config.field_map_overrides`. Pipeline fields are omitted from each
table (see §2).

### 3.1 `trips` → Notion **Trips**

| Field | Type | Notes |
|---|---|---|
| `title` | text required | Notion title |
| `date_start` | date | match key for reconcile |
| `date_end` | date | |
| `origin` | text | |
| `destination` | text | |
| `budget` | number | |
| `status` | select(1) | Planning / Booked / Ongoing / Done / Cancelled |
| `type` | select(1) | Leisure / Business / Family / Other |
| `content` | editor | long-form notes (Notion page body) |
| `related_plan` | relation→plans | PB-only this round |
| `companions` | relation→contacts (multi) | PB-only this round |

### 3.2 `days` → Notion **Day** (post-redesign: container only)

| Field | Type | Notes |
|---|---|---|
| `name` | text required | Notion title |
| `date` | date | match key |
| `weather` | text | NEW in migration 19 |
| `note` | text | daily summary line |
| `content` | editor | long-form day text |
| `trip` | relation→trips | PB-only |

**Dormant on Notion side after Phase 3**: `Reserved, Checkin, Amount,
Currency, Rate, Amount Usd, Activity Type, Score, Location, Actual Lat,
Actual Lng`. Sync stops touching them; user deletes from Notion UI when
convenient. (Field `migrated_to_stop_id` is temporary, removed by
migration 21.)

### 3.3 `stops` → Notion **Stops** (NEW in migration 18)

The atomic event under a day. See
[docs/superpowers/specs/2026-06-03-stops-redesign-design.md](superpowers/specs/2026-06-03-stops-redesign-design.md)
for full rationale.

| Field | Type | Notes |
|---|---|---|
| `name` | text required | agent-generated, e.g. "拉面店 @ 新宿" |
| `date` | date required | |
| `reserved` | date | scheduled / booking time (PB `date` stores datetime) |
| `checkin` | date | actual arrival time |
| `categories` | select(maxSelect=8) | multi-select: `打卡, 酒店, 餐厅, 购物, 体验, 交通, 笔记, 消费`. Notion sees `multi_select` (codec handles maxSelect>1 → multi_select). |
| `amount` | number | |
| `currency` | select(1) | JPY / EUR / USD / CNY / 其他 |
| `rate` | number | exchange rate used to compute amount_usd |
| `amount_usd` | number | reporting currency total |
| `note` | text | short stop-level comment ("汤太咸") |
| `actual_lat` | number | GPS captured at checkin |
| `actual_lng` | number | |
| `day` | relation→days | PB-only this round |
| `trip` | relation→trips | PB-only — redundant convenience |
| `location` | relation→locations | PB-only |
| `contact` | relation→contacts | PB-only |
| `journal` | relation→journal | PB-only — long-form note |

**Soft category conventions** (NOT schema-enforced):
- `打卡 / 酒店 / 餐厅 / 购物 / 体验` typically carry `location`
- `笔记` typically carries `journal`
- `消费` carries no relation — just an amount; can stack with another category
- `交通` flexible — flight/train carry `location`; pure delay-noting uses `journal`

### 3.4 `plans` → Notion **Plans**

| Field | Type | Notes |
|---|---|---|
| `title` | text required | Notion title |
| `target_date` | date | match key |
| (other plans fields per migration 13) | | |

### 3.5 `todos` → Notion **Todos**

| Field | Type | Notes |
|---|---|---|
| `title` | text required | Notion title |
| `due_date` | date | match key |
| (other todos fields per migration 8) | | |

### 3.6 `contacts` → Notion **Contacts**

| Field | Type | Notes |
|---|---|---|
| `name` | text required | Notion title |
| (other contacts fields per migration 7) | | |

No date field — match by name only.

### 3.7 `locations` → Notion **Location**

| Field | Type | Notes |
|---|---|---|
| `name` | text required | Notion title |
| `address` | text | |
| `city` | text | |
| `phone` | text | |
| `type` | select(1) | 餐馆 / 超市 / 咖啡馆 / 酒店 / 景点 / 商场 / 机场-车站 / 户外 / 其他 |
| `rating` | select(1) | ⭐ to ⭐⭐⭐⭐⭐ |
| `visited` | bool | |
| `lat`, `lng` | number | indexed `(lat, lng)` |
| `osm_id`, `amap_poi_id`, `fsq_id` | text | unique-when-non-empty indexes per source (OSM / 高德 / Foursquare) |
| `content` | editor | |

### 3.8 `journal` → Notion **Journal** (NEW sync target in migration 20)

| Field | Type | Notes |
|---|---|---|
| `title` | text required | Notion title |
| `date` | date | |
| `mood` | select(1) | Happy / Sad / Anxious / Excited / Calm / Frustrated / Grateful / Reflective / Energized |
| `type` | select(1) | Learning / Feeling / Observation / Event / Diary / **Reminder** (Reminder added in migration 20; UI displays as "注意" in Chinese) |
| `tags` | select(5) | 工作 / 家人 / 学习 / 读书 / 生活 (multi, up to 5) |
| `content` | editor | long-form |
| `related_trip` | relation→trips | PB-only |
| `related_day` | relation→days | PB-only |
| `related_stop` | relation→stops | NEW in migration 20, PB-only |

---

## 4. Sync Activity (Notion-only DB)

The user-facing queue for things needing a human decision. Lives in Notion
so the user browses natively and clicks `record_link` to jump to the
affected row. DB UUID is in `env: NOTION_SYNC_ACTIVITY_DB_ID`.

| Property | Type | Holds |
|---|---|---|
| `title` | title | "{op} · {summary[:60]}" |
| `op` | select | Conflict / Delete? / Possible duplicate / Schema mismatch |
| `direction` | select | Notion→PB / PB→Notion / Both / None |
| `collection` | select | which sync target |
| `record_link` | url | direct Notion link to the affected page |
| `pb_id` | rich_text | PB record id |
| `notion_id` | rich_text | Notion page id |
| `summary` | rich_text | one-line diff |
| `pb_snapshot` | rich_text | JSON of the PB row at detection |
| `notion_snapshot` | rich_text | JSON of Notion page (after `notion_page_to_pb_dict`) |
| `decision` | select | Pending → Use Notion / Use PB / Delete both / Keep both / Merge |
| `detected_at` | date | when runner first noticed |
| `applied_at` | date | when applier executed the decision (empty = pending) |
| `notes` | rich_text | user-editable scratch |

**Decision flow:**
1. Runner detects a conflict → writes Sync Activity row with `decision=Pending`,
   freezes both sides via `frozen_pairs_for_collection()`.
2. User sets `decision` in Notion UI.
3. Next runner pass → `apply_pending_decisions()` reads decisions, executes
   them (`_apply_one_decision`), stamps `applied_at`.
4. Row unfreezes, normal sync resumes for that pair.
5. After 90 days the resolved row is archived by `cleanup_resolved_activity`.

**Silent paths** don't touch Sync Activity:
- PbOnlyChange / NotionOnlyChange (single-side update)
- PbNew / NotionNew (creation)

---

## 5. Meta collections (PB-only)

### 5.1 `sync_config` — one row per synced target

```
sync_config {
  collection           text unique     // 'trips' | 'days' | ... | 'stops' | 'journal'
  notion_db_id         text required   // Notion DB UUID
  enabled              bool            // off-switch per collection
  field_map_overrides  json            // {"NotionColumnName": "pb_field_name"}; default {}
  last_synced_at       date            // updated after each pass
  last_sync_summary    text            // human-readable last result
  created/updated      autodate
}
```

**Lookup**: `pb.list_records("sync_config", filter="enabled=true")`.
**Add a new sync target**: insert one row + add pipeline fields to the
target collection (see §6 below) + create Notion DB with pb_id + last_synced_at
columns.

### 5.2 `sync_global` — single row of cross-collection settings

```
sync_global {
  timezone         text required   // 'America/New_York' etc. (zoneinfo name)
  sync_hour_local  number required // hour (0-23) at which the daily pass fires
  paused           bool            // global kill-switch
  last_run_at      date            // wall-clock of last actual pass
}
```

The hourly systemd timer fires `python -m notion_sync.runner`, which exits
silently unless `sync_global.paused == false` AND local time hour matches
`sync_hour_local` (see `runner.py::should_run_now`). The `--force-now` flag
bypasses the time + pause guard.

---

## 6. Codec rules (PB ↔ Notion field-level translation)

Lives in `notion_sync/codec.py`. Two functions:

- `pb_field_to_notion_property(value, *, pb_type, notion_type=None, max_select=1)` → dict
- `notion_property_to_pb_field(prop, *, pb_type, max_select=1)` → value

**Mapping table** (PB type → Notion type when `notion_type` not pinned):

| PB type | Notion type | Notes |
|---|---|---|
| text | rich_text | trimmed to 2000 chars |
| editor | rich_text | same |
| email | email | |
| url | url | |
| number | number | |
| bool | checkbox | |
| date | date | PB `YYYY-MM-DD HH:MM:SS.SSSZ` → Notion `YYYY-MM-DD` (time portion dropped) |
| select maxSelect=1 | select | |
| select maxSelect>1 | multi_select | — used by `stops.categories`, `journal.tags`, `trips.companions` (relation but treated as multi) |
| relation | relation | **NOT SYNCED** — see §7 |
| json | rich_text | JSON-serialized |

When `notion_type` is passed (the destination DB schema told us the actual
column type), the codec respects it — so a PB `text` field can be encoded
as `phone_number` if the Notion column is phone-typed.

**Field name conversion** (`codec.py:36-43`):
- `snake_to_title("departure_time")` → `"Departure Time"`
- `title_to_snake("Departure Time")` → `"departure_time"`

If your collection has a Notion column whose name doesn't round-trip
cleanly (e.g., `"AmountUSD"`), add an entry to
`sync_config.field_map_overrides`:
```json
{ "AmountUSD": "amount_usd" }
```

---

## 7. Known limitations

### 7.1 Relations are NOT synced bidirectionally

`notion_sync/transform.py` skips `relation`-typed fields in both directions
(`notion_page_to_pb_dict` lines 40-46; `pb_record_to_notion_props` lines
70-72). The reason: PB stores PB record ids in its relation field; Notion
stores Notion page UUIDs. The two ID spaces don't match, so a blind copy
would write garbage.

**Affected fields today**:
- `trips.related_plan`, `trips.companions`
- `days.location` *(removed in Phase 3)*
- `stops.day, stops.trip, stops.location, stops.contact, stops.journal`
- `journal.related_trip, journal.related_day, journal.related_stop`

What this means in practice:
- PB-side: relations are first-class and reliable
- Notion-side: relation columns (if you create them) stay empty
- The user sees full relation graph in PB; Notion shows scalar fields only

**Fix path** (future PR): build a lookup table `{collection, pb_id} ↔
{notion_page_id}` from each row's pipeline fields, then in the codec
translate relation arrays in both directions.

### 7.2 No Notion-side backup

Notion's API can't trigger a workspace backup. PB has `backup.py`. The
sync system mitigates by:
- Never destructive-writing to Notion without a Sync Activity entry first
- All "Delete both" decisions go through `apply_pending_decisions`, which
  is auditable
- User exports critical Notion DBs to CSV from the UI for major operations

### 7.3 Journal `type` enum mixes English + Chinese display

`type` values are English (`Learning, Feeling, Observation, Event, Diary,
Reminder`) for parity with the original schema. UI / agent displays
`Reminder` as "注意" in Chinese contexts. **Don't translate the stored
value** — only the display.

### 7.4 `date` fields lose time on round-trip to Notion

PB stores datetime in `date` fields, but Notion's date property serializes
to `YYYY-MM-DD` (without time) on read. So `stops.reserved` and
`stops.checkin` round-trip with time intact on the PB side, but their
Notion-displayed values are date-only. Time is preserved in PB; agents
operating on times should read from PB, not from Notion-via-sync.

---

## 8. Sync runner pipeline (high-level)

`notion_sync/runner.py` per pass (`--force-now` or the hourly tick at
`sync_hour_local`):

```
for each enabled sync_config row C:
    cleanup_resolved_activity(C.collection, days=90)    # archive old SA rows

    pb_rows     = pb.list_records(C.collection)
    notion_pages = nc.query_database(C.notion_db_id)
    frozen       = frozen_pairs_for_collection(C.collection)  # blocked by Pending SA

    actions = categorize(pb_rows, notion_pages, since=C.last_synced_at)
              # yields NoChange / PbOnlyChange / NotionOnlyChange / BothChanged
              # / PbNew / NotionNew / PbVanished / NotionVanished

    for a in actions where _action_ids(a) not in frozen:
        match a:
            PbOnlyChange     → _apply_pb_to_notion       # silent
            NotionOnlyChange → _apply_notion_to_pb       # silent
            PbNew            → _apply_pb_new             # silent, links back
            NotionNew        → _apply_notion_new         # silent, links back
            BothChanged      → write_conflict           # Sync Activity, freeze
            NotionVanished   → write_delete_question    # Sync Activity, freeze
            PbVanished       → write_delete_question    # Sync Activity, freeze

    apply_pending_decisions(C.collection)               # user-set decisions

    pb.update_record("sync_config", C.id, {
        "last_synced_at": now_iso_datetime(),
        "last_sync_summary": <pass counts>,
    })

if any_conflicts_or_deletes_were_written_this_pass:
    notify_pending()    # creates an in-app PB chat session
```

---

## 9. For agents: common operations

### 9.1 Add a new PB collection as a sync target

1. Create PB collection (a new `pb_migrations/XXXX_create_<name>.js`).
2. In the same or a follow-up migration, add the 3 pipeline fields:
   `notion_id` (text, unique-when-non-empty), `notion_last_edited` (date),
   `last_synced_at` (date). Pattern: see
   `1779465617_add_sync_pipeline_fields.js` and migration 18 (which adds
   them inline in the create).
3. In Notion, create the destination DB. Required columns: a title prop,
   `pb_id` (rich_text), `last_synced_at` (date). Other columns must match
   the PB schema (snake_to_title-derived names, unless you'll add a
   field_map_overrides entry).
4. Insert one `sync_config` row: `{collection: "<name>", notion_db_id:
   "...", enabled: true, field_map_overrides: {}}`.
5. Run `scripts/reconcile_initial.py --only <name>` to push existing PB
   rows up + link them.
6. Add `<name>` to `TITLE_FIELD_BY_COLLECTION` and `DATE_FIELD_BY_COLLECTION`
   in both `runner.py:55-58` and `reconcile_initial.py:42-50` if title /
   date fields differ from the defaults.
7. Trigger a `--force-now` run to verify.

### 9.2 Debug a row that's not syncing

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a

# 1. Check if the row is frozen by a Pending Sync Activity entry.
.venv/bin/python -c "
from notion_sync.activity import frozen_pairs_for_collection
from notion_sync.pb_api import PBClient
from notion_sync.notion_api import NotionClient
pb, nc = PBClient(), NotionClient()
print(frozen_pairs_for_collection(nc, 'trips'))
"

# 2. Check pipeline fields on the PB side.
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
r = PBClient().get_record('trips', 'PB_RECORD_ID')
print('notion_id:', r.get('notion_id'))
print('notion_last_edited:', r.get('notion_last_edited'))
print('last_synced_at:', r.get('last_synced_at'))
print('updated:', r.get('updated'))
"

# 3. Force a run with verbose log.
.venv/bin/python -m notion_sync.runner --force-now --only trips
sudo journalctl -u notion-sync.service -n 100 --no-pager
```

### 9.3 Manually mark a Sync Activity decision

In the Notion Sync Activity DB, set `decision` to one of
`Use Notion / Use PB / Delete both / Keep both`. Next runner pass
(`--force-now` to skip wait) applies it via
`runner.py::apply_pending_decisions`.

### 9.4 Add a new category to `stops.categories`

1. Write a new PB migration that mutates `stops.categories.values` to
   append the new value (pattern: see migration 20's handling of
   `journal.type.values`).
2. Add the same value to the Notion Stops DB's `Categories` multi-select
   property (Notion UI).
3. No codec change needed — multi_select envelope handles arbitrary values.

### 9.5 Update field_map_overrides

If you add a Notion column whose name doesn't snake-case to its PB
counterpart cleanly:

```bash
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
row = next(r for r in pb.list_records('sync_config') if r['collection'] == 'trips')
overrides = row.get('field_map_overrides') or {}
overrides['Trip Type'] = 'type'   # Notion col name → PB field name
pb.update_record('sync_config', row['id'], {'field_map_overrides': overrides})
"
```
