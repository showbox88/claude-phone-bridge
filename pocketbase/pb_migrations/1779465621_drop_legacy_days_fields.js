/// <reference path="../pb_data/types.d.ts" />
//
// Phase 3 (subtractive): finalize the days→days+stops redesign by removing
// the 11 fields that moved to stops + the temporary migration marker.
// Days becomes a pure container after this runs.
//
// IMPORTANT — DO NOT apply this until Phase 2 (the data migration script
// scripts/migrate_days_to_stops.py) has been run and verified. The
// migration includes a safety check that throws if any legacy atomic-day
// row (activity_type set) still lacks migrated_to_stop_id; this keeps the
// migration transactional so a half-migrated DB cannot lose data.
//
// See docs/superpowers/specs/2026-06-03-stops-redesign-design.md §6 Phase 3.
//
migrate((app) => {
  // Safety check — refuse to drop columns if Phase 2 hasn't been run.
  // Any days row with a non-empty activity_type and empty
  // migrated_to_stop_id is a legacy atomic-day row whose data is NOT
  // yet in stops; dropping fields would destroy it.
  const unmigrated = app.findRecordsByFilter(
    "days",
    "activity_type != '' && migrated_to_stop_id = ''",
    "",     // sort
    1000,   // limit (we only need to know there's at least one)
    0,      // offset
  );
  if (unmigrated.length > 0) {
    throw new Error(
      "Refusing to drop legacy days fields: " + unmigrated.length +
      " day row(s) still have activity_type set but no " +
      "migrated_to_stop_id. Run scripts/migrate_days_to_stops.py first."
    );
  }

  const c = app.findCollectionByNameOrId("days");
  const toRemove = [
    "reserved", "checkin",
    "amount", "currency", "rate", "amount_usd",
    "activity_type", "score",
    "location", "actual_lat", "actual_lng",
    "migrated_to_stop_id",
  ];
  for (const name of toRemove) {
    const f = c.fields.getByName(name);
    if (f) c.fields.removeById(f.id);
  }

  const idxToRemove = ["idx_days_location", "idx_days_activity_type"];
  c.indexes = (c.indexes || []).filter(
    (s) => !idxToRemove.some((n) => s.includes(n))
  );

  app.save(c);
}, (app) => {
  // Down migration: re-add fields (empty — no data restoration).
  const c = app.findCollectionByNameOrId("days");
  const locations = app.findCollectionByNameOrId("locations");

  const specs = [
    { name: "reserved",            type: "date" },
    { name: "checkin",             type: "date" },
    { name: "amount",              type: "number" },
    { name: "currency",            type: "select", maxSelect: 1,
      values: ["JPY", "EUR", "USD", "CNY", "其他"] },
    { name: "rate",                type: "number" },
    { name: "amount_usd",          type: "number" },
    { name: "activity_type",       type: "select", maxSelect: 1,
      values: ["景点观光", "爬山/徒步", "用餐", "购物", "休息", "交通", "娱乐", "其他"] },
    { name: "score",               type: "number", min: 0, max: 10 },
    { name: "location",            type: "relation",
      collectionId: locations.id, maxSelect: 1, cascadeDelete: false },
    { name: "actual_lat",          type: "number" },
    { name: "actual_lng",          type: "number" },
    { name: "migrated_to_stop_id", type: "text", max: 100 },
  ];
  for (const spec of specs) {
    if (!c.fields.getByName(spec.name)) {
      c.fields.add(new Field(spec));
    }
  }

  c.indexes = [
    ...(c.indexes || []),
    "CREATE INDEX idx_days_location ON days (location)",
    "CREATE INDEX idx_days_activity_type ON days (activity_type)",
  ];

  app.save(c);
});
