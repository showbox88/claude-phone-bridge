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
    let touched = false;
    for (const spec of PIPELINE_FIELDS) {
      if (c.fields.getByName(spec.name)) continue;
      c.fields.add(new Field(spec));
      touched = true;
    }
    const idxName = `idx_${name}_notion_id`;
    const indexes = [...(c.indexes || [])];
    if (!indexes.some((s) => s.includes(idxName))) {
      c.indexes = [...indexes,
        `CREATE UNIQUE INDEX ${idxName} ON ${name} (notion_id) WHERE notion_id != ''`,
      ];
      touched = true;
    }
    if (touched) app.save(c);
  }
}, (app) => {
  for (const name of SYNC_TARGETS) {
    try {
      const c = app.findCollectionByNameOrId(name);
      let touched = false;
      for (const fname of ["notion_id", "notion_last_edited", "last_synced_at"]) {
        const f = c.fields.getByName(fname);
        if (f) { c.fields.removeById(f.id); touched = true; }
      }
      const idxName = `idx_${name}_notion_id`;
      const before = c.indexes || [];
      const after = before.filter((s) => !s.includes(idxName));
      if (after.length !== before.length) { c.indexes = after; touched = true; }
      if (touched) app.save(c);
    } catch (e) {}
  }
});
