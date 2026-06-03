/// <reference path="../pb_data/types.d.ts" />
//
// Additive prep for days→days+stops redesign. Two new fields on `days`:
//   • weather (text) — kept long-term; days becomes a container with
//     daily-level fields like weather + note.
//   • migrated_to_stop_id (text) — temporary marker used by
//     scripts/migrate_days_to_stops.py for idempotency. Dropped in the
//     subtractive cleanup migration (Phase 3) once data is in stops.
//
// See docs/superpowers/specs/2026-06-03-stops-redesign-design.md §6 Phase 1.
//
migrate((app) => {
  const c = app.findCollectionByNameOrId("days");
  if (!c.fields.getByName("weather")) {
    c.fields.add(new Field({ name: "weather", type: "text" }));
  }
  if (!c.fields.getByName("migrated_to_stop_id")) {
    c.fields.add(new Field({ name: "migrated_to_stop_id", type: "text", max: 100 }));
  }
  app.save(c);
}, (app) => {
  const c = app.findCollectionByNameOrId("days");
  for (const name of ["weather", "migrated_to_stop_id"]) {
    const f = c.fields.getByName(name);
    if (f) c.fields.removeById(f.id);
  }
  app.save(c);
});
