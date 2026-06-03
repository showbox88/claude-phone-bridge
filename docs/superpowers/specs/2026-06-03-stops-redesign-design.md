# Trip data model redesign: days → days + stops

**Date**: 2026-06-03
**Status**: Design — pending implementation plan
**Owner**: showbox88
**Scope**: PocketBase schema + Notion DB structure for trip-related data. Sync
runner / activity / decision applier code is **not** touched in this round.

---

## 1. Problem

The current `days` collection is being used as if each row were a single
*activity* (one location visit, one meal, one transport leg). The fields
reflect that — `reserved`, `checkin`, `activity_type`, `amount/currency/...`,
`location`, `actual_lat/lng` — all describe one event, not one day.

This means:

- A real day in a trip (transport in the morning + lunch + sightseeing +
  shopping + dinner + mood note) needs N "day" rows on the same date — the
  noun "day" no longer matches the concept.
- "All locations visited today" can't be queried sensibly; you have to dedupe
  across N day-rows.
- Adding new event types (体验 / 笔记 / 注意) means either piling more values
  onto `activity_type` or stuffing them into `note`.

The fix is to introduce **`stops`** as the atomic event, and promote
**`days`** to a pure container parallel to `trips`.

```
trips ──< days ──< stops ──→ locations
                       └──→ contacts
                       └──→ journal
```

---

## 2. Schema (final)

### 2.1 `stops` (new collection)

```
stops {
  name              text required           // agent-generated, e.g. "拉面店 @ 新宿"
  date              date required
  reserved          date (with time)        // planned arrival / booking time
  checkin           date (with time)        // actual arrival / start time
  categories        select maxSelect>1      // see §3
  amount            number
  currency          select [JPY,EUR,USD,CNY,其他]
  rate              number
  amount_usd        number
  note              text                    // short stop-level comment ("汤太咸")
  actual_lat        number
  actual_lng        number

  // relations — PB-only this round (see §5)
  day               relation→days       maxSelect=1
  trip              relation→trips      maxSelect=1   // redundant convenience
  location          relation→locations  maxSelect=1
  contact           relation→contacts   maxSelect=1
  journal           relation→journal    maxSelect=1   // long-form note

  // sync pipeline (parity with PR1-3)
  notion_id            text
  notion_last_edited   date
  last_synced_at       date

  created/updated   autodate
}

indexes:
  CREATE INDEX        idx_stops_date     ON stops (date)
  CREATE INDEX        idx_stops_day      ON stops (day)
  CREATE INDEX        idx_stops_trip     ON stops (trip)
  CREATE INDEX        idx_stops_location ON stops (location)
  CREATE INDEX        idx_stops_contact  ON stops (contact)
  CREATE UNIQUE INDEX idx_stops_notion_id ON stops (notion_id) WHERE notion_id != ''
```

**Rationale for choosing this shape (B1 from brainstorm):**
- `categories` as multi_select expresses "what kinds of thing this stop is".
  A single stop can be `[餐厅, 心情]` (ate at place + recorded feelings) or
  `[体验, 消费]` (paid activity).
- 3 nullable relation slots (`location` / `contact` / `journal`) — one per
  related DB, not one per category. A stop never has two locations or two
  journal entries, so one slot per target DB is sufficient.
- `note` field stays on stop for quick short comments. Long-form writing
  goes to `journal` via the relation.

### 2.2 `days` (changed — gutted)

**Drop**: `reserved`, `checkin`, `amount`, `currency`, `rate`, `amount_usd`,
`activity_type`, `score`, `location`, `actual_lat`, `actual_lng`
(all moved to stops, or removed as redundant).

**Add**: `weather` (text, free-form e.g. "晴", "雨后转晴")

**Keep**: `name`, `date`, `note`, `content`, `trip`, pipeline fields,
`created/updated`.

```
days {
  name              text required
  date              date
  weather           text                    // NEW
  note              text                    // daily summary line
  content           editor                  // existing — long-form day content
  trip              relation→trips maxSelect=1
  notion_id, notion_last_edited, last_synced_at
  created/updated
}
```

Daily totals (e.g. total spent) are **not** stored — they're aggregated from
stops on read.

### 2.3 `journal` (changed — light extension)

**Add**:
- `related_stop` relation → stops, maxSelect=1
- Type enum value: `"Reminder"` (appended to existing
  `[Learning, Feeling, Observation, Event, Diary]`).
  Kept English for consistency with existing values; UI / agent displays as
  "注意" in Chinese contexts. Display localization is a UI concern, not schema.
- Sync pipeline fields: `notion_id`, `notion_last_edited`, `last_synced_at`

Everything else (mood, tags, content, related_trip, related_day) unchanged.

---

## 3. Categories enumeration

`stops.categories` is a multi-select with 8 values:

| Category | Typical relation | Example |
|---|---|---|
| `打卡` | location | 景点、纪念碑、地标到此一游 |
| `酒店` | location | 入住、退房 |
| `餐厅` | location (+ contact) | 吃饭，约朋友 |
| `购物` | location | 商店、纪念品 |
| `体验` | location (+ contact) | 旅行团、按摩、骑单车、看演出、上课 |
| `交通` | location (+ note) | 上车、下车、班机延误 |
| `笔记` | journal | 心情记录、注意事项、流水叙述 |
| `消费` | (no relation) | 单纯一笔花销，可叠加在其他 category 上 |

**Soft conventions** (not enforced by schema):
- `打卡 / 酒店 / 餐厅 / 购物 / 体验` 通常应带 `location`
- `笔记` 通常应带 `journal`
- `消费` 不需要任何 relation，但通常会和其他 category 叠加
- `交通` 灵活 — 班机/车次有具体位置时挂 location，单纯延误等情况挂 journal

---

## 4. Examples

| Real-world event | Stop row |
|---|---|
| 京都一日团（导游 8 千日元） | `categories=[体验,消费]`, `location=集合点`, `contact=导游`, `journal=评价`, `amount=8000, currency=JPY` |
| 拉面店吃饭（¥1200，汤太咸） | `categories=[餐厅,消费]`, `location=拉面店`, `note="汤太咸"`, `amount=1200, currency=JPY` |
| 街边冰淇淋（¥300，没地方） | `categories=[餐厅,消费]`, `amount=300, currency=JPY` |
| 网约骑单车（¥1500） | `categories=[体验,消费]`, `location=租车点`, `journal=路线记录`, `amount=1500` |
| 按摩 60 分钟（¥6000） | `categories=[体验,消费]`, `location=按摩店`, `amount=6000` |
| 朋友家吃饭 | `categories=[餐厅]`, `contact=朋友`, `journal="阿姨做的菜"` |
| 新干线 19:00 起飞实际 20:30 | `categories=[交通]`, `location=东京站`, `reserved=19:00`, `checkin=20:30`, `journal="延误"` |
| 今天注意防晒 | `categories=[笔记]`, `journal=备忘 entry(type=注意)` |

---

## 5. Sync impact

### 5.1 What stays the same

- `sync_global` / hourly cron / paused gate — untouched
- `sync_config` infrastructure (per-collection rows, field_map_overrides) — untouched
- Sync Activity queue + conflict / delete detection — untouched
- Decision applier (`apply_pending_decisions`) — untouched, works generically
- trips / plans / todos / contacts / locations — schema and sync untouched

### 5.2 What changes

- `days` Notion DB loses meaning for the dropped properties. They're
  **left in Notion as dormant columns** (sync just stops writing them).
  User can manually delete those columns from Notion at leisure. The remaining
  days sync (name, date, weather, note, content, trip) continues to work
  bidirectionally.
- New Notion DBs: **Stops** and **Journal** (created manually by the user).
- Two new `sync_config` rows: `stops` and `journal`.

### 5.3 Known limitation (carried over from PR2)

`notion_sync/transform.py` **skips all relation fields in both directions**
(lines 40–46 and 70–72). This means:

- `stops.day / trip / location / contact / journal` will sync as data on the
  PB side only. The Notion Stops DB will have empty relation columns (or
  the user can choose not to add those columns to Notion at all this round).
- `journal.related_trip / related_day / related_stop` likewise PB-only.
- Scalar fields (date, time, categories, amount, currency, note, etc.) do
  sync bidirectionally normally.

This is acceptable for now: the user just brought sync online and wants to
lock down structure first, then iterate on sync features. **Relation sync
(PB id ↔ Notion page id translation) is explicitly out of scope for this
redesign and tracked as a future PR.**

---

## 6. Migration plan (existing data)

### Phase 0 — Safety

- Set `sync_global.paused = true`
- Run existing `backup.py` to snapshot both PB and Notion
- Verify backups before proceeding

### Phase 1 — PB additive schema (sync still works after)

New migration(s):

1. Create `stops` collection with full schema from §2.1 (including pipeline fields).
2. Add `weather` text field to `days`.
3. Add `migrated_to_stop_id` text field to `days` (temporary — dropped in Phase 3;
   used for migration idempotency).
4. Add `related_stop` relation to `journal`; extend `journal.type` enum to
   include `"Reminder"`.
5. Add `notion_id`, `notion_last_edited`, `last_synced_at` to `journal`
   (extend the `SYNC_TARGETS` list in `1779465617_add_sync_pipeline_fields.js`
   pattern via a new migration that targets `["journal"]` and `["stops"]`).

After Phase 1: sync can resume safely — no fields removed yet, so existing
`days ↔ Day` sync path is unaffected.

### Phase 2 — Data migration script (idempotent)

`scripts/migrate_days_to_stops.py` (new):

```
for each row d in days:
  s = create stops row with:
    name        = d.name
    date        = d.date
    reserved    = d.reserved
    checkin     = d.checkin
    amount      = d.amount
    currency    = d.currency
    rate        = d.rate
    amount_usd  = d.amount_usd
    categories  = activity_type_to_categories(d.activity_type)
    location    = d.location
    actual_lat  = d.actual_lat
    actual_lng  = d.actual_lng
    note        = d.note
    trip        = d.trip

  // canonical day container
  key = (d.trip, d.date)
  if key not yet seen:
    canonical[key] = d.id    // reuse this row as the container
  s.day = canonical[key]

  // dedupe: if this row is the 2nd+ on (trip, date), schedule for deletion
  // (its data is now in s); we don't actually delete here — manual review step

  if d.id != canonical[key]:
    mark d for deletion (output to review CSV)
```

`activity_type_to_categories` mapping:

| Old `activity_type` | New `categories` |
|---|---|
| 景点观光 | `[打卡]` |
| 爬山/徒步 | `[体验]` |
| 用餐 | `[餐厅]` |
| 购物 | `[购物]` |
| 休息 | `[酒店]` (in trip context, "rest" almost always = back at hotel) |
| 交通 | `[交通]` |
| 娱乐 | `[体验]` |
| 其他 | `[]` |

**Idempotency mechanism**: Phase 1 also adds a temporary text field
`migrated_to_stop_id` to `days`. The migration script:

- For each day row `d`: if `d.migrated_to_stop_id != ""`, skip (already done).
- After successfully creating the stops row `s`, set
  `d.migrated_to_stop_id = s.id`.

Phase 3 drops `migrated_to_stop_id` along with the other moved fields.

The script also:
- supports `--dry-run` (prints plan, writes nothing)
- outputs a review CSV listing day-rows that would be deleted as duplicates
  on the same `(trip, date)` key
- requires explicit `--apply-delete` flag to actually delete those duplicate
  day rows (default is keep — user reviews CSV first)

### Phase 3 — PB subtractive schema

New migration: drop from `days`: `reserved`, `checkin`, `amount`, `currency`,
`rate`, `amount_usd`, `activity_type`, `score`, `location`, `actual_lat`,
`actual_lng`, `migrated_to_stop_id`. Drop their indexes.

After Phase 3: `days` is the lean container.

### Phase 4 — Notion-side setup (manual + script)

User-side manual:
1. In Notion, create **Stops** DB with properties matching §2.1 scalar fields
   (date, reserved, checkin, categories[multi_select 8 values], amount,
   currency[select], rate, amount_usd, note, actual_lat, actual_lng, pb_id,
   last_synced_at).
2. In Notion, create **Journal** DB with properties matching journal scalar
   fields.
3. Get the two new Notion DB UUIDs and insert sync_config rows in PB:
   ```
   { collection: "stops",   notion_db_id: "...", enabled: true }
   { collection: "journal", notion_db_id: "...", enabled: true }
   ```

Script-side:
- Run `scripts/reconcile_initial.py --only stops` and `--only journal` to push
  PB rows to Notion and populate pb_id back-links.

### Phase 5 — Resume sync, verify

- `sync_global.paused = false`
- `.venv/bin/python -m notion_sync.runner --force-now`
- Spot-check:
  - new stops appearing in Notion Stops DB
  - day Notion pages still have name/date/weather/note/content correctly
  - dormant columns in Day Notion DB unchanged (acceptable)
- Optional: user manually deletes dormant columns from Notion Day DB

---

## 7. Out of scope (future PRs)

- **Relation sync** (PB id ↔ Notion page id translation). This is a meaningful
  feature on its own — needs a lookup table built from each side's pipeline
  fields, plus codec extension. Tracked separately.
- **Auto-fill on Notion side**: e.g. show stops as a Notion view inside
  the day page. Possible via Notion views/filters; configuration is user-side.
- **Daily totals / rollups in Notion**: Notion's native rollup property could
  surface "total spent today" from stops — set up by user, not by sync.
- **Stop title auto-generation**: agent does this in PB-side MCP tools.
  No codec involvement.

---

## 8. Implementation order (rough sketch — full plan in next doc)

1. PR-A: Phase 1 migrations (additive PB schema)
2. PR-B: Phase 2 migration script + dry-run testing on backup
3. PR-C: Phase 2 apply on production data
4. PR-D: Phase 3 migrations (subtractive PB schema)
5. PR-E: Phase 4 Notion-side reconcile + sync_config rows
6. PR-F: Phase 5 verification + cleanup

PRs A and D are pure schema (small). B is the most careful — needs the
review CSV to be reviewed by hand. C is a one-shot prod operation. E
involves Notion manual setup + a reconcile run. F is observation.

Detailed implementation plan to be written in
`docs/superpowers/plans/2026-06-03-stops-redesign-implementation.md` after
this design is approved.
