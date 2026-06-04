/// <reference path="../pb_data/types.d.ts" />
//
// Add a SECOND nullable sync hour to sync_global so the runner can fire
// twice per local day (default: 03 = overnight + 15 = mid-afternoon). The
// runner ORs the two hours together; null/empty disables the slot.
//
// Idempotent: skips field if already present, skips seed if row already
// has a value.
//
migrate((app) => {
  const c = app.findCollectionByNameOrId("sync_global");
  if (!c.fields.getByName("sync_hour_local_2")) {
    c.fields.add(new Field({ name: "sync_hour_local_2", type: "number" }));
    app.save(c);
  }
  // Seed the existing single row with 15:00 local as a sensible default.
  const rows = app.findRecordsByFilter("sync_global", "");
  for (const row of rows) {
    if (row.get("sync_hour_local_2") == null || row.get("sync_hour_local_2") === "") {
      row.set("sync_hour_local_2", 15);
      app.save(row);
    }
  }
}, (app) => {
  const c = app.findCollectionByNameOrId("sync_global");
  const f = c.fields.getByName("sync_hour_local_2");
  if (f) { c.fields.removeById(f.id); app.save(c); }
});
