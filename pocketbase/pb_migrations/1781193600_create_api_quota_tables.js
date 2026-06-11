/// <reference path="../pb_data/types.d.ts" />
//
// API quota tables — system_settings (toggle/limit config), api_logs (per-call
// audit trail), system_alerts (operator alerts). Used by the Smart-Trip
// apiGuard to throttle external API usage and surface incidents.
//
migrate((app) => {
  // system_settings — key/value config (toggles, limits).
  const settings = new Collection({
    name: "system_settings",
    type: "base",
    listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: "",
    fields: [
      { name: "key",     type: "text", required: true, max: 100 },
      { name: "value",   type: "text", max: 500 },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_system_settings_key ON system_settings (key)",
    ],
  });
  app.save(settings);

  // Seed default toggles and limits.
  const seeds = [
    ["places_search_enabled", "true"],
    ["place_details_enabled", "true"],
    ["directions_enabled",    "true"],
    ["daily_api_limit",       "200"],
    ["per_2min_api_limit",    "20"],
  ];
  for (const [k, v] of seeds) {
    const r = new Record(settings, { key: k, value: v });
    app.save(r);
  }

  // api_logs — one row per external API call (audit / quota counting).
  const logs = new Collection({
    name: "api_logs",
    type: "base",
    listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: "",
    fields: [
      { name: "api_type", type: "text", required: true, max: 50 },
      { name: "user_id",  type: "text", max: 50 },
      { name: "status",   type: "text", required: true, max: 20 },
      { name: "created",  type: "autodate", onCreate: true, onUpdate: false },
    ],
    indexes: [
      "CREATE INDEX idx_api_logs_type_status_created ON api_logs (api_type, status, created)",
    ],
  });
  app.save(logs);

  // system_alerts — operator-facing incidents (quota breaches, etc.).
  const alerts = new Collection({
    name: "system_alerts",
    type: "base",
    listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: "",
    fields: [
      { name: "kind",         type: "text", required: true, max: 50 },
      { name: "api_type",     type: "text", max: 50 },
      { name: "reason",       type: "text", max: 50 },
      { name: "count",        type: "number" },
      { name: "acknowledged", type: "bool" },
      { name: "created",      type: "autodate", onCreate: true, onUpdate: false },
    ],
    indexes: [
      "CREATE INDEX idx_system_alerts_ack_created ON system_alerts (acknowledged, created)",
    ],
  });
  app.save(alerts);
}, (app) => {
  for (const name of ["system_alerts", "api_logs", "system_settings"]) {
    try { app.delete(app.findCollectionByNameOrId(name)); } catch (e) {}
  }
});
