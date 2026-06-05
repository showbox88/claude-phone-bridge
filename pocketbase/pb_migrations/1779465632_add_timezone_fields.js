/// <reference path="../pb_data/types.d.ts" />
//
// Adds IANA timezone fields across the trip stack so cross-tz reminders
// and history can anchor to local time:
//
//   locations.timezone (text)        - computed from lat/lng at creation
//   stops.timezone     (text)        - denormalized from location or GPS
//   days.timezone      (text)        - inherited from day's first stop
//   expenses.timezone  (text)        - inherited from parent stop/day
//   foods.timezone     (text)        - inherited from parent stop/day
//   todos.due_at       (date)        - reminder trigger time (UTC)
//   todos.due_tz       (text)        - IANA name of user's intended tz
//
// See docs/superpowers/specs/2026-06-05-timezone-design.md.
//
migrate((app) => {
  const TXT_TABLES = ["locations", "stops", "days", "expenses", "foods"];
  for (const name of TXT_TABLES) {
    const c = app.findCollectionByNameOrId(name);
    if (c.fields.getByName("timezone")) continue;
    c.fields.add(new Field({
      name: "timezone",
      type: "text",
      max:  64,
    }));
    app.save(c);
  }

  const todos = app.findCollectionByNameOrId("todos");
  if (!todos.fields.getByName("due_at")) {
    todos.fields.add(new Field({
      name: "due_at",
      type: "date",
    }));
  }
  if (!todos.fields.getByName("due_tz")) {
    todos.fields.add(new Field({
      name: "due_tz",
      type: "text",
      max:  64,
    }));
  }
  const existing = todos.indexes || [];
  const idxDef = "CREATE INDEX idx_todos_due_at ON todos (due_at)";
  if (!existing.some((s) => s.includes("idx_todos_due_at"))) {
    todos.indexes = [...existing, idxDef];
  }
  app.save(todos);
}, (app) => {
  const TXT_TABLES = ["locations", "stops", "days", "expenses", "foods"];
  for (const name of TXT_TABLES) {
    const c = app.findCollectionByNameOrId(name);
    const f = c.fields.getByName("timezone");
    if (f) {
      c.fields.removeById(f.id);
      app.save(c);
    }
  }
  const todos = app.findCollectionByNameOrId("todos");
  for (const fname of ["due_at", "due_tz"]) {
    const f = todos.fields.getByName(fname);
    if (f) todos.fields.removeById(f.id);
  }
  todos.indexes = (todos.indexes || []).filter((s) => !s.includes("idx_todos_due_at"));
  app.save(todos);
});
