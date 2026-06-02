/// <reference path="../pb_data/types.d.ts" />
//
// Add Notion sync pipeline fields to the 6 collections that will be synced
// to Notion. Idempotent: skips fields that already exist (so re-running on
// a partially-migrated DB is safe).
//
const SYNC_TARGETS = ["trips", "days", "plans", "todos", "contacts", "locations"];

const PIPELINE_FIELDS = [
  { name: "notion_id",          type: "text", max: 100 },
  { name: "notion_last_edited", type: "date" },
  { name: "last_synced_at",     type: "date" },
];

migrate((app) => {
  for (const name of SYNC_TARGETS) {
    const c = app.findCollectionByNameOrId(name);
    const existing = new Set(c.fields.map((f) => f.name));
    let touched = false;
    for (const spec of PIPELINE_FIELDS) {
      if (existing.has(spec.name)) continue;
      c.fields.push(new Field(spec));
      touched = true;
    }
    const idxName = `idx_${name}_notion_id`;
    if (!c.indexes.some((s) => s.includes(idxName))) {
      c.indexes.push(`CREATE UNIQUE INDEX ${idxName} ON ${name} (notion_id) WHERE notion_id != ''`);
      touched = true;
    }
    if (touched) app.save(c);
  }
}, (app) => {
  for (const name of SYNC_TARGETS) {
    try {
      const c = app.findCollectionByNameOrId(name);
      c.fields = c.fields.filter((f) => !["notion_id", "notion_last_edited", "last_synced_at"].includes(f.name));
      c.indexes = c.indexes.filter((s) => !s.includes(`idx_${name}_notion_id`));
      app.save(c);
    } catch (e) {}
  }
});
