/// <reference path="../pb_data/types.d.ts" />
//
// Sync meta collections — per-collection config and global config for the
// Notion <-> PB sync pipeline. Created by PR1. Cron logic lands in PR2.
//
migrate((app) => {
  // sync_config — one row per collection that gets synced to Notion.
  const cfg = new Collection({
    name: "sync_config",
    type: "base",
    listRule: null, viewRule: null, createRule: null, updateRule: null, deleteRule: null,
    fields: [
      { name: "collection",            type: "text", required: true, max: 100 },
      { name: "notion_db_id",          type: "text", required: true, max: 100 },
      { name: "enabled",               type: "bool" },
      { name: "field_map_overrides",   type: "json", maxSize: 100000 },
      { name: "last_synced_at",        type: "date" },
      { name: "last_sync_summary",     type: "text", max: 1000 },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_sync_config_collection ON sync_config (collection)",
    ],
  });
  app.save(cfg);

  // sync_global — single-row global settings (timezone, sync hour, paused).
  const glb = new Collection({
    name: "sync_global",
    type: "base",
    listRule: null, viewRule: null, createRule: null, updateRule: null, deleteRule: null,
    fields: [
      { name: "timezone",        type: "text", required: true, max: 100 },
      { name: "sync_hour_local", type: "number", required: true },
      { name: "paused",          type: "bool" },
      { name: "last_run_at",     type: "date" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
  });
  app.save(glb);

  // Seed one sync_global row with sensible defaults.
  const initial = new Record(glb, {
    timezone: "America/New_York",
    sync_hour_local: 3,
    paused: false,
  });
  app.save(initial);
}, (app) => {
  for (const name of ["sync_config", "sync_global"]) {
    try { app.delete(app.findCollectionByNameOrId(name)); } catch (e) {}
  }
});
