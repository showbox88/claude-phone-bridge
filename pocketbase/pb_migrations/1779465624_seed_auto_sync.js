/// <reference path="../pb_data/types.d.ts" />
//
// Follow-up to 1779465623: that migration's seed loop guarded on
// auto_sync === null, but PB materializes new bool columns as false (not
// null), so the guard never fired and every row landed with
// auto_sync=false. Fix: unconditionally set auto_sync=true for the 6 rows
// where that's the intended seed. plans + contacts stay false (which
// matches the design intent — they already have it from the bool default).
//
// Idempotent: re-running this migration is a no-op for rows already at
// the target value.
//
migrate((app) => {
  const TRUE_ROWS = ["trips", "todos", "journal", "days", "locations", "stops"];
  const rows = app.findRecordsByFilter("sync_config", "");
  for (const row of rows) {
    if (TRUE_ROWS.indexOf(row.get("collection")) === -1) continue;
    if (row.get("auto_sync") !== true) {
      row.set("auto_sync", true);
      app.save(row);
    }
  }
}, (app) => {
  // No-op down: this is a data fix, not a schema change. The schema
  // rollback lives in 1779465623's down handler.
});
