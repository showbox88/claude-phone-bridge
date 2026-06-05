/// <reference path="../pb_data/types.d.ts" />
//
// Relax days.trip from required → optional. Needed so the expenses data
// migration can auto-create day containers for old transactions that have
// no trip (日常消费).
//
// See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md.
//
migrate((app) => {
  const c = app.findCollectionByNameOrId("days");
  const f = c.fields.getByName("trip");
  if (!f) throw new Error("days.trip field missing");
  f.required = false;
  app.save(c);
}, (app) => {
  const c = app.findCollectionByNameOrId("days");
  const f = c.fields.getByName("trip");
  if (f) {
    f.required = true;
    app.save(c);
  }
});
