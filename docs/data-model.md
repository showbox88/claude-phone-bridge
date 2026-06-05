# Phone Bridge: trip data model & workflow

Canonical reference for the trip-related data in Phone Bridge — covers
both PocketBase (source of truth) and Notion (mirror), the sync mechanics
between them, and concrete workflow examples.

**Audience**: future agents working on sync, migration, MCP tools,
or new features that touch trip data.

Last verified: 2026-06-03 (post stops redesign Phase 1–5).

---

## Table of contents

1. [Hierarchy](#1-hierarchy)
2. [PocketBase side — all trip-touching collections](#2-pocketbase-side)
3. [Notion side — mirror DBs](#3-notion-side)
4. [Field mapping: PB ↔ Notion](#4-field-mapping)
5. [Sync pipeline](#5-sync-pipeline)
6. [Sync Activity (the decision queue)](#6-sync-activity)
7. [Trip workflows — concrete examples](#7-trip-workflows)
8. [Known limitations](#8-known-limitations)
9. [Operations cookbook for agents](#9-operations-cookbook)
10. [Quick reference: all IDs](#10-quick-reference-all-ids)

---

## 1. Hierarchy

```
trips ──< days ──< stops ──→ locations
   │        │        ├──→ contacts
   │        │        └──→ journal ──→ trips, days, stops
   │        │
   │        └─── (Notion only: dormant historical relations)
   │
   └──< plans      ── soft-link "trip is part of plan"
   └──< todos      ── todos are NOT trip-bound today
   └──< companions ── trip.companions (multi) → contacts
```

**Semantic roles**:
- **`trip`** — a planned period of travel (Tokyo Oct–Dec 2026 etc.). Has a
  start/end date, a budget, a status. Container for days.
- **`day`** — one calendar date in a trip. Container for stops. Carries
  only daily-level info: name, date, weather, daily note, long-form
  content. **No activity-level fields** (those live on stop).
- **`stop`** — an atomic event inside a day (a flight leg, a meal, a
  museum visit, a mood note, a single expense). Carries the actual
  activity data. Tagged with one or more `categories`.
- **`location`** — a place (restaurant, hotel, landmark). Stops link to
  locations; locations are reusable across trips.
- **`contact`** — a person (companion, guide, host). Stops can link to
  contacts.
- **`journal`** — long-form writing (mood note, diary entry, observation,
  reminder). A stop can attach a journal entry for richer content than
  `stop.note`'s one-liner.
- **`plan`** — higher-level life plans; trips can link `related_plan`.

**The split between day and stop** is the heart of the design. Before
2026-06-03 a `day` row carried activity data and you needed N day-rows
per real calendar day. After the redesign, one real calendar day = one
day row + N stops. See
[docs/superpowers/specs/2026-06-03-stops-redesign-design.md](superpowers/specs/2026-06-03-stops-redesign-design.md)
for full rationale.

---

## 2. PocketBase side

PocketBase is **source of truth**. All structural changes flow PB → Notion.
PB lives at `/opt/pocketbase/pocketbase` listening on `127.0.0.1:8090`.
Working dir: `/opt/pocketbase/`, migrations in `/opt/pocketbase/pb_migrations/`.

Migrations under `phone-bridge/pocketbase/pb_migrations/` are auto-copied
to `/opt/pocketbase/pb_migrations/` by the deploy script (`pre_restart`
step) and PB is restarted to apply them.

### 2.1 `trips`

```
trips {
  id                     text (PB-generated 15-char id)
  title                  text required, max 500
  date_start             date
  date_end               date
  origin                 text
  destination            text
  budget                 number
  status                 select(1) [Planning, Booked, Ongoing, Done, Cancelled]
  type                   select(1) [Leisure, Business, Family, Other]
  content                editor (rich text / page body)
  related_plan           relation→plans (single)
  companions             relation→contacts (multi, up to 999)
  notion_id              text  (unique-when-non-empty)
  notion_last_edited     date
  last_synced_at         date
  created                autodate
  updated                autodate
}

indexes:
  CREATE INDEX  idx_trips_date_start  ON trips (date_start)
  CREATE INDEX  idx_trips_date_end    ON trips (date_end)
  CREATE INDEX  idx_trips_status      ON trips (status)
  CREATE INDEX  idx_trips_related_plan ON trips (related_plan)
  CREATE UNIQUE INDEX idx_trips_notion_id ON trips (notion_id) WHERE notion_id != ''
```

### 2.2 `days` (container only — post-redesign)

```
days {
  id                     text
  name                   text required, max 500
  date                   date
  weather                text     // free-form: "晴", "雨后转晴"
  note                   text     // daily summary one-liner
  content                editor   // long-form day text
  timezone               text, max 64   // IANA name; see Timezone section
  trip                   relation→trips (single, OPTIONAL — relaxed in migration 1779465625)
  photos                 (whatever the existing photos field was — preserved)
  notion_id              text (unique-when-non-empty)
  notion_last_edited     date
  last_synced_at         date
  created                autodate
  updated                autodate
}

indexes:
  CREATE INDEX  idx_days_date         ON days (date)
  CREATE INDEX  idx_days_trip         ON days (trip)
  CREATE UNIQUE INDEX idx_days_notion_id ON days (notion_id) WHERE notion_id != ''
```

**Removed in Phase 3 (migration 21)**: `reserved`, `checkin`, `amount`,
`currency`, `rate`, `amount_usd`, `activity_type`, `score`, `location`,
`actual_lat`, `actual_lng`, `migrated_to_stop_id`. All migrated to
`stops`.

### 2.3 `stops` (new in migration 18)

```
stops {
  id                     text
  name                   text required, max 500     // agent-generated, e.g. "拉面店 @ 新宿"
  date                   date required
  reserved               date     // planned arrival / booking time (datetime)
  checkin                date     // actual arrival time (datetime)
  categories             select(maxSelect=8)
                         [打卡, 酒店, 餐厅, 购物, 体验, 交通, 笔记, 消费]
  // amount/currency/rate/amount_usd REMOVED in migration 1779465628 —
  // money fields moved to `expenses` collection (see §2.9). A stop can have
  // 0..N expenses linked via expense.stop.
  note                   text     // short comment ("汤太咸")
  timezone               text, max 64   // IANA name; see Timezone section
  actual_lat             number
  actual_lng             number
  day                    relation→days       (single)
  trip                   relation→trips      (single — redundant convenience for queries)
  location               relation→locations  (single)
  contact                relation→contacts   (single)
  journal                relation→journal    (single — long-form note)
  notion_id              text (unique-when-non-empty)
  notion_last_edited     date
  last_synced_at         date
  created                autodate
  updated                autodate
}

indexes:
  CREATE INDEX  idx_stops_date        ON stops (date)
  CREATE INDEX  idx_stops_day         ON stops (day)
  CREATE INDEX  idx_stops_trip        ON stops (trip)
  CREATE INDEX  idx_stops_location    ON stops (location)
  CREATE INDEX  idx_stops_contact     ON stops (contact)
  CREATE UNIQUE INDEX idx_stops_notion_id ON stops (notion_id) WHERE notion_id != ''
```

**Soft category conventions** (NOT enforced):
| Category | Typical relation | Example |
|---|---|---|
| `打卡` | location | 景点、地标 |
| `酒店` | location | 入住、退房 |
| `餐厅` | location (+ contact) | 吃饭 |
| `购物` | location | 商店、纪念品 |
| `体验` | location (+ contact) | 旅行团、按摩、骑单车、看演出 |
| `交通` | location 和/或 journal | 班机、车次、延误 |
| `笔记` | journal | 心情、注意、叙述 |
| `消费` | (无 relation) | 单纯一笔花销，可叠加在其它 category 上 |

### 2.4 `locations`

```
locations {
  id, name (required), address, city, phone,
  type (select 1) [餐馆, 超市, 咖啡馆, 酒店, 景点, 商场, 机场/车站, 户外, 其他]
  rating (select 1) [⭐...⭐⭐⭐⭐⭐]
  visited (bool)
  lat, lng (number)
  osm_id, amap_poi_id, fsq_id (text, unique-when-non-empty per source)
  content (editor)
  timezone (text, max 64)  // IANA name; see Timezone section
  notion_id, notion_last_edited, last_synced_at, created, updated
}

indexes:
  unique osm_id / amap_poi_id / fsq_id (each WHERE != '')
  (lat, lng) compound, type, city, notion_id
```

### 2.5 `contacts`

```
contacts {
  id, name (required),
  (other fields per migration 7 — name is the only required field)
  notion_id, notion_last_edited, last_synced_at, created, updated
}
```

### 2.6 `journal` (extended in migration 20)

```
journal {
  id,
  title (text required, max 500),
  date (date),
  mood (select 1) [Happy, Sad, Anxious, Excited, Calm, Frustrated, Grateful, Reflective, Energized]
  type (select 1) [Learning, Feeling, Observation, Event, Diary, Reminder]
  tags (select up to 5) [工作, 家人, 学习, 读书, 生活]
  content (editor)
  related_trip (relation→trips, single)
  related_day  (relation→days, single)
  related_stop (relation→stops, single)        // NEW in migration 20
  notion_id, notion_last_edited, last_synced_at (NEW pipeline fields)
  created, updated
}

indexes:
  idx_journal_date, idx_journal_mood
  CREATE UNIQUE INDEX idx_journal_notion_id ON journal (notion_id) WHERE notion_id != ''
```

`type=Reminder` is the English value for what's displayed as "注意" in
Chinese UI. Don't translate the stored value.

### 2.10 `foods` (joined sync 2026-06-05 via migration 1779465631)

Atomic "dish I ate". A meal at a restaurant or a street food snack
creates 1 stop (category=餐厅) + N foods rows (one per dish) + 0..N
expenses (the bill, possibly split). foods.location is older / optional;
when stop is set, `stop.location` is the source of truth and
`foods.location` is redundant convenience.

```
foods {
  id, dish (required, max 500),
  price (number), currency (select 1) [JPY, EUR, USD, CNY, 其他],
  flavor (multi, maxSelect=6) [辣, 甜, 咸, 酸, 清淡, 油腻],
  rating (select 1) [❤️ ... ❤️❤️❤️❤️❤️],
  want_again (bool),
  content (editor),
  photos (json),
  timezone (text, max 64),                          // IANA name; see Timezone section
  location (relation→locations, single, optional),
  stop     (relation→stops,    single, optional),   // 2026-06-05
  day      (relation→days,     single, optional),   // 2026-06-05
  trip     (relation→trips,    single, optional, denormalized = day.trip),
  notion_id, notion_last_edited, last_synced_at, created, updated
}

indexes:
  idx_foods_location, idx_foods_rating,
  idx_foods_stop, idx_foods_day, idx_foods_trip,
  UNIQUE idx_foods_notion_id WHERE != ''
```

**Use cases**:
- "今天吃了什么" → `foods where day = today_day_id`
- "京都 trip 美食汇总" → `foods where trip = T`
- "这家店点过什么" → `foods where location = L` (across visits) or
  `foods where stop = S` (one visit)

### 2.7 `plans` / `todos`

`plans` and `todos` are synced but not central to trip flow. Trips link
to plans (`trip.related_plan`); todos can attach to a `stop` / `day` /
`trip` (all optional, added in migration 1779465630) the same way
expenses do, so a checklist like "pre-trip prep" or "before visiting
the temple" can hang off the relevant container. Same writer-side
convention: `todo.trip` mirrors `todo.day.trip` when day has a trip.

Other todo fields are in migrations 8/13 + 1779465629 (the `icon`
text field that carries the Notion page emoji).

Timezone-aware reminder fields (added 2026-06-05):

```
todos {
  ...
  due_at                 date           // reminder trigger (UTC)
  due_tz                 text, max 64   // IANA tz user expressed time in
  ...
}
```

### 2.9 `expenses` (new in migration 1779465626 — replaces `transactions`)

Money child of stops/days/trips. The legacy `transactions` collection
(migration 11) was dropped in migration 1779465627; its 11 rows migrated
here via `scripts/migrate_transactions_to_expenses.py`. The 4 money
fields previously on `stops` (amount/currency/rate/amount_usd) were
dropped in migration 1779465628; their data migrated via
`scripts/migrate_stops_money_to_expenses.py`.

```
expenses {
  id                     text
  description            text required, max 500
  amount                 number       // in `currency`; refunds stored negative
  currency               select(1) [USD, JPY, EUR, CNY, 其他]
  rate                   number       // 1 unit foreign ≈ N USD; empty for USD
  amount_usd             number       // writer-side auto-filled (= amount if USD, else amount × rate)
  date                   date
  type                   select(1) [支出, 退款]
  expense_category       select(1) [旅行, 订阅服务, 娱乐, 交通, 购物/日用,
                                    餐饮, 门票, 住宿, 代付, 其他]
  card                   select(1) [Chase Sapphire Preferred (7675)]
  confirmation           text         // Gmail receipt dedup key (unique-when-non-empty)
  source                 select(1) [手动, Gmail, Agent]
  timezone               text, max 64 // IANA name; see Timezone section
  stop                   relation→stops    (single, optional)
  day                    relation→days     (single, optional)
  trip                   relation→trips    (single, optional, denormalized = day.trip)
  notion_id              text (unique-when-non-empty)
  notion_last_edited     date
  last_synced_at         date
  created                autodate
  updated                autodate
}

indexes:
  CREATE INDEX idx_expenses_date     ON expenses (date)
  CREATE INDEX idx_expenses_category ON expenses (expense_category)
  CREATE INDEX idx_expenses_stop     ON expenses (stop)
  CREATE INDEX idx_expenses_day      ON expenses (day)
  CREATE INDEX idx_expenses_trip     ON expenses (trip)
  CREATE UNIQUE INDEX idx_expenses_confirmation ON expenses (confirmation) WHERE confirmation != ''
  CREATE UNIQUE INDEX idx_expenses_notion_id    ON expenses (notion_id)    WHERE notion_id != ''
```

**Conventions** (enforced by writer, NOT by PB):
- `amount_usd` auto-filled at write time. USD rows: `amount_usd = amount`,
  `rate = empty/0`. Foreign rows: `amount_usd = amount × rate`.
- Refunds (`type='退款'`) stored with `amount < 0` so `sum(amount_usd)` is
  net spend without a CASE branch.
- `expense.trip == expense.day.trip` when `day.trip` is set. If a day's
  trip changes, all expenses under it must be cascaded by the writer
  (no PB hook today).
- Relations (stop/day/trip) are PB-only — sync (when wired) ignores them
  per §8.1.

**Use cases**:
- One stop can hold N expenses (park visit → 门票 + 冰淇淋 + 水)
- Daily expenses without a trip: `expense.day` set to today's day row
  (which has `trip=empty`); `expense.trip` also empty
- Heatmap / weekly / monthly / yearly summaries: group by `date` / `expense_category`
- Trip totals: `sum where trip = T`; trip-vs-daily compare: `trip is null` filter

### 2.8 Meta collections (PB-only, not synced)

```
sync_config {
  collection           text unique     // 'trips' | 'days' | ... | 'stops' | 'journal'
  notion_db_id         text required   // Notion DB UUID
  enabled              bool
  field_map_overrides  json            // {"NotionColumnName": "pb_field_name"}; default {}
  last_synced_at       date
  last_sync_summary    text
  title_field          text            // (added 2026-06-04) PB field used as the Notion
                                       // title column. Required. Seeded by migration
                                       // 1779465623 (e.g. trips → "title", days → "name").
  date_field           text            // (added 2026-06-04) PB field used for ordering /
                                       // fuzzy matching during reconcile. Empty for
                                       // contacts and locations.
  auto_sync            bool            // (added 2026-06-04) When true, a PB write via
                                       // mcp__pb__pb_create / pb_update / pb_delete
                                       // schedules a 10-second-debounced runner pass
                                       // for this collection. When false, the row waits
                                       // for the next cron tick.
}

sync_global {                          // single row
  timezone         text required   // 'America/New_York' (zoneinfo name)
  sync_hour_local  number required // 0-23
  paused           bool            // global kill-switch
  last_run_at      date
}
```

---

## 3. Notion side

All databases live under the **Smart Note** page
(`369acd0fbb8980c8ac72fdab06e709c4`). Each Notion database has one
data source (collection). The sync runner targets databases by UUID
(stored in `sync_config.notion_db_id`).

### 3.1 Trips — database

- Database id: `df7ea062-7b18-4c4f-98f1-bfec8258c3db`
- Data source id: `2e5ca117-baef-4cbf-9031-01e7cccf0d9c`

| Property | Type | Notion column → PB field |
|---|---|---|
| Title | title | `title` |
| Dates | date | (NOT round-tripped — PB stores `date_start`/`date_end` separately) |
| Origin | rich_text | `origin` |
| Destination | rich_text | `destination` |
| Budget | number ($) | `budget` |
| Status | select | `status` |
| Type | select | `type` |
| Related Plan | relation → Plans data source | `related_plan` *(PB-only sync)* |
| Companions | relation → Contacts | `companions` *(PB-only sync)* |
| Related to Trip Stops (Trip) | relation reverse | auto from `stops.trip` *(PB-only sync)* |
| Related to Journal (Related Trip) | relation reverse | auto from `journal.related_trip` *(PB-only sync)* |
| pb_id | rich_text | sync linker |
| last_synced_at | date | sync linker |

### 3.2 Day — database (container only)

- Database id: `13329dea-4f55-4fc8-8e64-6c1ff19353bb`
- Data source id: `2220c9f9-4eb3-4df4-b3a0-a7b14f2cf064`

| Property | Type | Notion → PB |
|---|---|---|
| Name | title | `name` |
| Date | date | `date` |
| Weather | rich_text | `weather` (added 2026-06-03) |
| Note | rich_text | `note` |
| Trip | relation → Trips | `trip` *(PB-only sync)* |
| Related to Journal (Related Day) | relation reverse | auto from `journal.related_day` |
| pb_id | rich_text | sync linker |
| last_synced_at | date | sync linker |

Historical "Activity type / Amount / Currency / Rate / Check-in / Reserved /
Score / Location / Amount (USD) formula" columns were dropped 2026-06-03.
All that data moved to `stops`.

### 3.3 Stops — database (new 2026-06-03)

- Database id: `15bb0429-a026-48b4-96f8-4447d5060ee3`
- Data source id: `2f485c77-b15f-40cb-aa58-28e9cfac7e64`
- Views:
  - `Default view` (table)
  - `📅 时间线` — table, sorted by Date ascending
  - `🏷️ 按类型` — board, grouped by Categories
  - `🌍 按行程` — table, grouped by Trip, sorted by Date

| Property | Type | Notion → PB |
|---|---|---|
| Name | title | `name` |
| Date | date | `date` |
| Reserved | date | `reserved` *(datetime; Notion shows date-only on read)* |
| Checkin | date | `checkin` *(datetime)* |
| Categories | multi_select [打卡, 酒店, 餐厅, 购物, 体验, 交通, 笔记, 消费] | `categories` |
| Amount | number | `amount` |
| Currency | select [JPY, EUR, USD, CNY, 其他] | `currency` |
| Rate | number | `rate` |
| Amount Usd | number | `amount_usd` |
| Note | rich_text | `note` |
| Actual Lat | number | `actual_lat` |
| Actual Lng | number | `actual_lng` |
| Day | relation → Day | `day` *(PB-only sync)* |
| Trip | relation → Trips | `trip` *(PB-only sync)* |
| Location | relation → Locations | `location` *(PB-only sync)* |
| Contact | relation → Contacts | `contact` *(PB-only sync)* |
| Journal | relation → Journal | `journal` *(PB-only sync)* |
| pb_id | rich_text | sync linker |
| last_synced_at | date | sync linker |

### 3.4 Plans / Todos / Contacts / Location — databases

| PB | Notion DB id | Data source id | Title field |
|---|---|---|---|
| `plans`     | `c951c7a9-a8f5-4ffd-aea2-1244e437ae46` | (fetch on demand) | `title` |
| `todos`     | `5d4e3f93-cf13-4707-97c5-59b38940baac` | (fetch on demand) | `title` |
| `contacts`  | `e304a6c3-4771-4c69-9ffc-97a672a1ac0c` | `caca728e-a3be-4758-aadc-ad26fd6b339f` | `name` |
| `locations` | `257c34c1-ac50-455d-9c8a-8d810de5c1e5` | `bd067a50-2dcf-44ed-8c43-e9c80925cff3` | `name` |

### 3.5 Journal — database

- Database id: `ccc3b239-682d-47a1-a20e-e33b3c8fae44`
- Data source id: `2711f877-d03b-4702-88e0-5db59093c532`

| Property | Type | Notion → PB |
|---|---|---|
| Title | title | `title` |
| Date | date | `date` |
| Mood | select | `mood` |
| Type | select [Learning, Feeling, Observation, Event, Diary, **Reminder**] | `type` |
| Tags | multi_select | `tags` |
| Related Trip | relation → Trips | `related_trip` *(PB-only sync)* |
| Related Day | relation → Day | `related_day` *(PB-only sync)* |
| pb_id | rich_text | sync linker (added 2026-06-03) |
| last_synced_at | date | sync linker (added 2026-06-03) |

`Reminder` option + sync pipeline columns were added 2026-06-03.

### 3.6 Sync Activity — database (decision queue)

- Database id: `373acd0f-bb89-81e2-9142-caaf3cac86f3`
- Data source id: `373acd0f-bb89-812e-92ea-000b5e80eab9`
- Env var: `NOTION_SYNC_ACTIVITY_DB_ID`

See [§6](#6-sync-activity) for full schema.

---

## 4. Field mapping

### 4.1 Auto-conversion

Field names auto-translate via two helpers in `notion_sync/codec.py:36-43`:

- `snake_to_title("departure_time")` → `"Departure Time"` (PB → Notion)
- `title_to_snake("Departure Time")` → `"departure_time"` (Notion → PB)

`title_to_snake` collapses both spaces and hyphens to underscores. So
`"Check-in"` would become `"check_in"` (no underscore on PB side: PB
uses `checkin`). **This is why Stops uses `"Checkin"` not `"Check-in"`
in Notion** — to round-trip cleanly with PB's `checkin`.

### 4.2 Type envelopes (`pb_field_to_notion_property` / `notion_property_to_pb_field`)

| PB type | Notion type | Notes |
|---|---|---|
| text | rich_text | trimmed to 2000 chars |
| editor | rich_text | same |
| email | email | |
| url | url | |
| number | number | |
| bool | checkbox | |
| date | date | PB `YYYY-MM-DD HH:MM:SS.SSSZ` → Notion `YYYY-MM-DD` (time portion dropped on read) |
| select maxSelect=1 | select | |
| select maxSelect>1 | multi_select | `stops.categories`, `journal.tags`, `trips.companions` |
| relation | relation | **NOT SYNCED** — see §8.1 |
| json | rich_text | JSON-serialized |

When the Notion column type is known (e.g. fetched from data source
schema), the codec uses *that* type instead of the PB-inferred one. So
a PB `text` field can be encoded as `phone_number` if the Notion column
is phone-typed.

### 4.3 Overrides

When auto-conversion fails (column name doesn't round-trip cleanly),
add an entry to `sync_config.field_map_overrides`:
```json
{ "AmountUSD": "amount_usd" }    // Notion col name → PB field name
```

---

## 5. Sync pipeline

`notion_sync/runner.py` runs hourly via systemd timer
`notion-sync.timer`. Exits silently unless local hour in
`sync_global.timezone` matches `sync_global.sync_hour_local`. The
`--force-now` CLI flag bypasses this.

Per pass, for each enabled `sync_config` row C:

```python
# 1. Cleanup
cleanup_resolved_activity(C.collection, days=90)
  # Archives Sync Activity rows whose applied_at < today - 90

# 2. Fetch both sides
pb_rows      = pb.list_records(C.collection)
notion_pages = nc.query_database(C.notion_db_id)
frozen       = frozen_pairs_for_collection(C.collection)
  # set of (pb_id, notion_id) currently blocked by Pending Sync Activity

# 3. Categorize
actions = categorize(pb_rows, notion_pages, since=C.last_synced_at)
  # yields one of:
  #   NoChange         | PbOnlyChange | NotionOnlyChange | BothChanged
  #   PbNew            | NotionNew
  #   PbVanished       | NotionVanished

# 4. Dispatch
for a in actions where _action_ids(a) not in frozen:
    if PbOnlyChange:     _apply_pb_to_notion(a)         # silent
    if NotionOnlyChange: _apply_notion_to_pb(a)         # silent
    if PbNew:            _apply_pb_new(a)               # silent, links back
    if NotionNew:        _apply_notion_new(a)           # silent, links back
    if BothChanged:      write_conflict(a)              # → Sync Activity, freeze pair
    if NotionVanished:   write_delete_question(a, side='notion')
    if PbVanished:       write_delete_question(a, side='pb')

# 5. Apply user decisions
apply_pending_decisions(C.collection)
  # reads Sync Activity rows with decision != Pending, applied_at empty;
  # executes the chosen action; stamps applied_at

# 6. Stamp
pb.update_record('sync_config', C.id, {
    'last_synced_at':    now_iso_datetime(),
    'last_sync_summary': f'runner: applied={n} conflicts={c} deletes={d}',
})

# 7. Notify if anything pending
if any_conflicts_or_deletes_written_this_pass:
    notify_pending()    # creates an in-app phone-bridge chat session "📋 同步待确认 N 项"
```

**Change detection** (`runner.py::categorize` + `notion_sync/changeset.py`):
- PB row "changed" iff `pb.updated > C.last_synced_at`
- Notion page "changed" iff `notion.last_edited_time > pb.notion_last_edited`
- "New" iff one side has no linker id pointing at the other side
- "Vanished" iff a linked id no longer resolves in the other side's results

**Silent paths** (no Sync Activity write):
- PbOnlyChange / NotionOnlyChange / PbNew / NotionNew

**Loud paths** (Sync Activity row created):
- BothChanged → `op=Conflict`, freezes the pair
- *Vanished → `op=Delete?`, freezes the pair

**Logs**:
- Structured JSONL: `/home/dev/phone-bridge/.bridge_data/sync.log`
- systemd: `journalctl -u notion-sync.service`

---

## 6. Sync Activity

The user-facing decision queue. Lives in **Notion** so the user clicks
through to records natively.

| Property | Type | Holds |
|---|---|---|
| `title` | title | `"{op} · {summary[:60]}"` |
| `op` | select | `Conflict` / `Delete?` / `Possible duplicate` / `Schema mismatch` / `Auto-applied` |
| `direction` | select | `Notion→PB` / `PB→Notion` / `Both` / `None` |
| `collection` | select | one of: trips, days, plans, todos, contacts, locations, **stops**, **journal** |
| `record_link` | url | direct Notion link to the affected page |
| `pb_id` | rich_text | PB record id |
| `notion_id` | rich_text | Notion page id |
| `summary` | rich_text | one-line diff |
| `pb_snapshot` | rich_text | JSON of PB row at detection time |
| `notion_snapshot` | rich_text | JSON of Notion page (after `notion_page_to_pb_dict`) |
| `decision` | select | `Pending` → user picks → `Use Notion` / `Use PB` / `Delete both` / `Keep both` / `Merge` / `N/A` |
| `detected_at` | date | when runner first noticed |
| `applied_at` | date | when decision was applied (empty = pending) |
| `notes` | rich_text | user-editable scratch |

**Decision flow**:
1. Runner detects → writes row with `decision=Pending`, freezes both sides
2. User opens row in Notion → sets `decision`
3. Next runner pass → `apply_pending_decisions()` executes → stamps `applied_at`
4. Row unfreezes, normal sync resumes
5. 90 days later, resolved row is archived by `cleanup_resolved_activity`

**`Keep both`** is a no-op (logs only) — useful for "yes both are
legitimate, leave them be". **`Merge`** is not currently implemented in
the applier (treated as N/A — user must resolve manually).

---

## 7. Trip workflows

These are the recurring patterns. Use these as templates when writing
MCP tools or agent flows.

### 7.1 Start a new trip

```
pb.create_record('trips', {
    'title': '京都 12 月',
    'date_start': '2026-12-10',
    'date_end':   '2026-12-20',
    'status': 'Planning',
    'type': 'Leisure',
    'budget': 3000,
    'origin': 'New York',
    'destination': '京都',
})
```

No Notion call needed. Next sync pass (or `--force-now`) will:
- See `PbNew`
- Create matching Notion page in Trips DB
- Write back `notion_id` to the PB trip row

### 7.2 Add a day to a trip

```
pb.create_record('days', {
    'name': '京都 Day 1 — 抵达',
    'date': '2026-12-10',
    'trip': '<trip_pb_id>',
    'weather': '阴',
})
```

Same as trips — sync auto-creates the Notion Day page. The `trip` relation
exists on PB side, but won't propagate to Notion (PR2 limitation). The
"Trip" column on Notion Day pages stays empty until relation-sync ships.

### 7.3 Record real events as they happen ("我刚在新宿吃了拉面")

The Phone Bridge agent should:
1. Identify or create the location:
   ```python
   matches = pb.list_records('locations',
       filter='name ~ "拉面" && city = "新宿"')
   if not matches:
       loc = pb.create_record('locations', {
           'name': '一風堂 新宿店',
           'city': '新宿',
           'type': '餐馆',
       })
   else:
       loc = matches[0]
   ```

2. Find (or create) the canonical day for today:
   ```python
   today_days = pb.list_records('days',
       filter=f'trip = "{trip_id}" && date = "2026-12-10"')
   if today_days:
       day = today_days[0]
   else:
       day = pb.create_record('days', {
           'name': '京都 Day 1',
           'date': '2026-12-10',
           'trip': trip_id,
       })
   ```

3. Create the stop:
   ```python
   pb.create_record('stops', {
       'name': '一風堂拉面 @ 新宿',
       'date': '2026-12-10',
       'checkin': '2026-12-10 12:30:00',  // optional, agent's best guess
       'categories': ['餐厅', '消费'],
       'amount': 1200,
       'currency': 'JPY',
       'note': '汤太咸',
       'day': day['id'],
       'trip': trip_id,
       'location': loc['id'],
   })
   ```

That's it. No Notion call needed in real-time. Within 1 hour (or on the
next `sync_now`) the stop appears in Notion.

### 7.4 Daily summary

```
day = pb.list_records('days', filter=f'trip = "{trip_id}" && date = "{today}"')[0]
pb.update_record('days', day['id'], {
    'note':    '累但开心。明天起早去清水寺。',
    'weather': '晴转多云',
})
```

Or — for long-form — create a journal entry:
```
pb.create_record('journal', {
    'title': '京都 Day 1 总结',
    'date':  '2026-12-10',
    'type':  'Diary',
    'mood':  'Happy',
    'related_trip': trip_id,
    'related_day':  day['id'],
    'content': '今天去了 ...（长文）',
})
```

### 7.5 Booked a flight ahead of time

```
pb.create_record('stops', {
    'name':       '日航 NH 6 JFK→HND',
    'date':       '2026-12-09',
    'reserved':   '2026-12-09 19:00:00',  // scheduled departure
    'checkin':    '',                      // empty until day-of
    'categories': ['交通', '消费'],
    'amount':     1112.94,
    'currency':   'USD',
    'rate':       1.0,
    'amount_usd': 1112.94,
    'day':        day_id,
    'trip':       trip_id,
})
```

On the actual flight day:
```
pb.update_record('stops', stop_id, {
    'checkin': '2026-12-09 20:30:00',     // actual departure (delayed)
})
# Optional: add a journal note
pb.create_record('journal', {
    'title': 'NH 6 delayed 1.5 hrs',
    'type':  'Reminder',
    'related_stop': stop_id,
    ...
})
```

### 7.6 Edit on Notion side

Open the Notion page, edit any column. Within 1 hour the runner:
- sees `NotionOnlyChange`
- pulls the new values into PB
- updates `pb.notion_last_edited` so the round trip doesn't loop

**Relation columns are ignored** — editing "Location" on a Notion Stops
row does nothing PB-side. Edit on PB (or wait for relation-sync PR).

### 7.7 Conflict resolution

Scenario: user edits the trip title in Notion AND in PB between syncs.

1. Runner detects `BothChanged` → writes Sync Activity row:
   - `op=Conflict`, `decision=Pending`
   - `pb_snapshot` + `notion_snapshot` carry both sides verbatim
2. Phone-bridge creates a chat session `"📋 同步待确认 1 项"`
3. User opens Sync Activity row in Notion, reviews snapshots, sets
   `decision = "Use Notion"` (or `Use PB` / `Keep both` / `Delete both`)
4. Next runner pass: `apply_pending_decisions` reads the row, executes
   the chosen action, stamps `applied_at`
5. Row unfreezes → normal sync resumes

### 7.8 Deletion

Same dance as conflict. Vanished side → Sync Activity `op=Delete?`,
frozen. User decides `Delete both` (propagate the deletion) or
`Keep both` (resurrect / restore link).

---

## 8. Known limitations

### 8.1 Relations: PB→Notion now synced, Notion→PB still skipped

**As of 2026-06-05** `notion_sync/transform.py` translates PB relation
ids to Notion page UUIDs by looking up each target collection's pipeline
`notion_id` field (`build_relation_lookup()` + `relation_target_collections()`).
The runner builds the lookup once per `sync_collection` pass and threads
it through `pb_record_to_notion_props`. `scripts/backfill_relations.py`
backfills existing pages one-off.

**PB→Notion** (works):
- All `relation` fields on synced collections write through to Notion's
  matching relation property when the target page exists (i.e. the target
  PB row has `notion_id` set). Brand new rows whose target wasn't yet
  synced in this pass appear empty on first write and fill in on the
  next pass.

**Notion→PB** (still skipped):
- `notion_page_to_pb_dict` still drops relation properties. Reason: same
  ID-space problem in the other direction. User edits to relation
  columns in Notion do not propagate to PB. Fix path: add the inverse
  lookup `{collection: {notion_id: pb_id}}` and translate symmetrically.

### 8.2 `date` fields lose time on Notion round-trip

PB stores datetime in `date`-typed fields. Notion `date` property reads
back as `YYYY-MM-DD` only. So `stops.reserved` and `stops.checkin`
round-trip with time on PB side, but Notion displays date-only. Time
is preserved in PB — read from PB if you need the precise time.

### 8.3 No Notion-side backup

Notion API can't trigger a workspace backup. PB has `backup.py`
(snapshots to `.bridge_data/backups/<ts>/`). The sync system mitigates by:
- Never destructive-writing to Notion without a Sync Activity row first
- All "Delete both" decisions go through the auditable applier
- User can manually export Notion DBs to CSV for major operations

### 8.4 Journal `type` mixes English + Chinese display

Stored values are English: `Learning, Feeling, Observation, Event,
Diary, Reminder`. UI / agent surfaces `Reminder` as "注意" in Chinese
contexts. Don't translate the stored value.

### 8.5 Day's dormant "Related to ..." reverse columns

The Notion Day DB has `Related to Journal (Related Day)` which is the
auto-generated reverse from `journal.related_day`. It stays in the
schema; the sync ignores it (it's a reverse view of a relation, not a
stored column). Cosmetic only.

---

## 9. Operations cookbook

### 9.1 Trigger an immediate sync

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -m notion_sync.runner --force-now
# OR for a single target:
.venv/bin/python -m notion_sync.runner --force-now --only stops
tail -50 .bridge_data/sync.log
```

Or via the in-app MCP tool: `mcp__pb__sync_now`.

### 9.2 Pause / resume sync

```bash
# Pause (every hourly tick skips, logs 'skipped_paused')
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
pb.update_record('sync_global', pb.list_records('sync_global', sort='')[0]['id'], {'paused': True})
"

# Resume
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
pb = PBClient()
pb.update_record('sync_global', pb.list_records('sync_global', sort='')[0]['id'], {'paused': False})
"
```

Or via MCP: `mcp__pb__sync_pause` / `mcp__pb__sync_resume`.

### 9.3 Debug "this row isn't syncing"

```bash
# 1. Frozen by Pending Sync Activity?
.venv/bin/python -c "
from notion_sync.notion_api import NotionClient
from notion_sync.activity import frozen_pairs_for_collection
print(frozen_pairs_for_collection(NotionClient(), 'stops'))
"

# 2. Pipeline fields on PB side
.venv/bin/python -c "
from notion_sync.pb_api import PBClient
r = PBClient().get_record('stops', 'STOP_ID')
print('notion_id:', r.get('notion_id'))
print('notion_last_edited:', r.get('notion_last_edited'))
print('last_synced_at:', r.get('last_synced_at'))
print('updated:', r.get('updated'))
"

# 3. Force a single-target run + check log
.venv/bin/python -m notion_sync.runner --force-now --only stops
tail -50 .bridge_data/sync.log
```

### 9.4 Add a new PB collection as a sync target

1. Write a new `pb_migrations/XXXX_create_<name>.js` that creates the
   collection AND includes the 3 pipeline fields (`notion_id`,
   `notion_last_edited`, `last_synced_at`). Pattern: see migration 18.
2. `deploy` — auto-applies the migration via the new `cp -u` step.
3. Create the Notion destination DB (via `mcp__notion__notion-create-database`
   or by hand). Required columns: a title prop, `pb_id` (rich_text),
   `last_synced_at` (date). Other columns should match the PB schema with
   names that `snake_to_title` produces.
4. Get the new Notion DB's UUID, insert sync_config row:
   ```bash
   .venv/bin/python -c "
   from notion_sync.pb_api import PBClient
   PBClient().create_record('sync_config', {
       'collection': '<name>',
       'notion_db_id': '<dashed-uuid>',
       'enabled': True,
       'field_map_overrides': {},
   })
   "
   ```
5. Add `<name>` to **both** `TITLE_FIELD_BY_COLLECTION` and
   `DATE_FIELD_BY_COLLECTION` in `notion_sync/runner.py` AND
   `scripts/reconcile_initial.py` if its title/date fields differ
   from `title`/empty.
6. **Add `<name>` to the Sync Activity DB's `collection` select options**
   (use `mcp__notion__notion-update-data-source ALTER COLUMN`). The
   runner queries `collection = '<name>'` and Notion 400s on unknown
   select values.
7. Run `scripts/reconcile_initial.py --only <name>` to push existing
   PB rows up + populate pb_id back-links.
8. `--force-now` to verify end-to-end.

### 9.5 Add a category to `stops.categories`

1. New PB migration mutates `stops.categories.values` (pattern: see
   migration 20's handling of `journal.type.values`).
2. Add the same value to the Notion Stops DB's Categories multi-select
   (via MCP `notion-update-data-source` or Notion UI).
3. No codec change. Multi-select envelope is generic.

### 9.6 Apply a Sync Activity decision manually

In Notion, set the row's `decision` to one of `Use Notion` / `Use PB` /
`Delete both` / `Keep both`. Next runner pass picks it up. Use
`--force-now` to apply immediately.

### 9.7 Update field_map_overrides

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

### 9.8 Backup before risky ops

```bash
.venv/bin/python -c "
from pathlib import Path
from notion_sync.pb_api import PBClient
from notion_sync.backup import backup_collections
print(backup_collections(PBClient(), Path('.bridge_data/backups')))
"
# Snapshot of every base collection lands in .bridge_data/backups/<ts>/<name>.json
```

---

## 10. Quick reference: all IDs

### PB sync targets (10)

| PB collection | Title field | Date field | Notion DB |
|---|---|---|---|
| `trips`     | title       | date_start  | `df7ea062-7b18-4c4f-98f1-bfec8258c3db` |
| `days`      | name        | date        | `13329dea-4f55-4fc8-8e64-6c1ff19353bb` |
| `stops`     | name        | date        | `15bb0429-a026-48b4-96f8-4447d5060ee3` |
| `expenses`  | description | date        | `376acd0f-bb89-815d-b137-f281c201f24e` |
| `foods`     | dish        | —           | `376acd0f-bb89-81cd-8023-c7058e208e43` |
| `plans`     | title       | target_date | `c951c7a9-a8f5-4ffd-aea2-1244e437ae46` |
| `todos`     | title       | due_date    | `5d4e3f93-cf13-4707-97c5-59b38940baac` |
| `contacts`  | name        | —           | `e304a6c3-4771-4c69-9ffc-97a672a1ac0c` |
| `locations` | name        | —           | `257c34c1-ac50-455d-9c8a-8d810de5c1e5` |
| `journal`   | title       | date        | `ccc3b239-682d-47a1-a20e-e33b3c8fae44` |

### Notion data source IDs (for MCP `notion-update-data-source` calls)

| DB | data_source_id |
|---|---|
| Trips     | `2e5ca117-baef-4cbf-9031-01e7cccf0d9c` |
| Day       | `2220c9f9-4eb3-4df4-b3a0-a7b14f2cf064` |
| Stops     | `2f485c77-b15f-40cb-aa58-28e9cfac7e64` |
| Locations | `bd067a50-2dcf-44ed-8c43-e9c80925cff3` |
| Contacts  | `caca728e-a3be-4758-aadc-ad26fd6b339f` |
| Journal   | `2711f877-d03b-4702-88e0-5db59093c532` |
| Sync Activity | `373acd0f-bb89-812e-92ea-000b5e80eab9` |

### Parent page (where all DBs live)

- **Smart Note**: `369acd0fbb8980c8ac72fdab06e709c4`

### Env vars (in `/home/dev/phone-bridge/.env`)

- `POCKETBASE_URL` — `http://127.0.0.1:8090`
- `POCKETBASE_ADMIN_EMAIL`, `POCKETBASE_ADMIN_PASSWORD` — PB auth
- `NOTION_TOKEN` — internal integration token
- `NOTION_SYNC_ACTIVITY_DB_ID` — `373acd0f-bb89-81e2-9142-caaf3cac86f3`

### Hosts / services

- `dashboard-server.tail4cfa2.ts.net` — Tailscale host
- `phone-bridge.service` — FastAPI on 127.0.0.1:8001
- `pocketbase.service` — PB on 127.0.0.1:8090, working dir `/opt/pocketbase/`
- `notion-sync.timer` — hourly cron firing `notion-sync.service`
- `mcp_pb.service` — claude.ai Custom Connector MCP server for PB tools

### Migration files (chronological)

| # | File | What it does |
|---|---|---|
| 01 | create_trips.js | trips |
| 02 | create_locations.js | locations |
| 03 | create_days.js | days (atomic — legacy shape) |
| 04 | create_foods.js | foods (PB-only) |
| 05 | create_journal.js | journal |
| 06 | extend_existing.js | adds content (editor) + fsq_id |
| 07 | create_contacts.js | contacts |
| 08 | create_todos.js | todos |
| 09 | create_daily_briefing.js | daily_briefing (PB-only) |
| 10 | create_claude_memos.js | claude_memos (PB-only) |
| 11 | create_transactions.js | transactions (PB-only) |
| 12 | create_ideas.js | ideas (PB-only) |
| 13 | create_plans.js | plans |
| 14 | link_trips_to_plans_contacts.js | trips.related_plan + companions |
| 15 | create_pages.js | pages (PB-only) |
| 16 | create_sync_meta.js | sync_config + sync_global |
| 17 | add_sync_pipeline_fields.js | adds notion_id/notion_last_edited/last_synced_at to 6 sync targets |
| 18 | create_stops.js | NEW stops collection |
| 19 | extend_days_for_stops_migration.js | days += weather + migrated_to_stop_id |
| 20 | extend_journal_for_stops.js | journal += related_stop + Reminder + pipeline fields |
| 21 | drop_legacy_days_fields.js | days -= 12 legacy fields |

---

## §11 Timezone resolution

All trip-stack collections carry an optional `timezone` column (IANA name).
Writer-side fallback chain (see
`docs/superpowers/specs/2026-06-05-timezone-design.md`):

1. `stop.timezone` — explicit on the stop (denormalized from location at write time)
2. `gps_to_tz(stop.actual_lat, stop.actual_lng)` — when GPS present
3. `day.timezone` — inherited from day
4. runtime client tz reported by phone-bridge
5. empty (leave for later patching)

Reminders (`todos.due_at` UTC + `todos.due_tz` IANA) anchor to the resolved tz
at write time; subsequent edits to the trip's tz do **not** retroactively
shift existing `due_at` values (the original intent is preserved by `due_tz`).

Notion-side: datetime columns are rendered with the row's `timezone` (or
`due_tz` for todos) as a `+HH:MM` offset so users see local time in Notion
directly. The IANA name itself is also synced as a plain text column.
