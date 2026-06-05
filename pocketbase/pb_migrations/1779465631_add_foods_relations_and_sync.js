/// <reference path="../pb_data/types.d.ts" />
//
// Add `stop` / `day` / `trip` single-relation optional fields to `foods`,
// plus the sync pipeline fields (notion_id / notion_last_edited /
// last_synced_at) so foods can join the Notion sync registry.
//
// Same writer-side convention as expense/todo:
// - foods.stop is the eating event
// - foods.day = foods.stop.day (when stop is set), else direct
// - foods.trip = foods.day.trip (when day has a trip)
//
// foods.location stays as it was (single relation → locations); it's
// redundant with stop.location when both are set, but harmless. Street
// food has stop with no location, so foods.location can be empty.
//
// See docs/data-model.md.
//
migrate((app) => {
  const c     = app.findCollectionByNameOrId("foods");
  const stops = app.findCollectionByNameOrId("stops");
  const days  = app.findCollectionByNameOrId("days");
  const trips = app.findCollectionByNameOrId("trips");

  const relationSpecs = [
    { name: "stop", collectionId: stops.id },
    { name: "day",  collectionId: days.id },
    { name: "trip", collectionId: trips.id },
  ];
  for (const spec of relationSpecs) {
    if (c.fields.getByName(spec.name)) continue;
    c.fields.add(new Field({
      name:          spec.name,
      type:          "relation",
      collectionId:  spec.collectionId,
      maxSelect:     1,
      cascadeDelete: false,
    }));
  }

  const pipelineSpecs = [
    { name: "notion_id",          type: "text", max: 100 },
    { name: "notion_last_edited", type: "date" },
    { name: "last_synced_at",     type: "date" },
  ];
  for (const spec of pipelineSpecs) {
    if (c.fields.getByName(spec.name)) continue;
    c.fields.add(new Field(spec));
  }

  const existing = c.indexes || [];
  const toAdd = [
    "CREATE INDEX idx_foods_stop ON foods (stop)",
    "CREATE INDEX idx_foods_day  ON foods (day)",
    "CREATE INDEX idx_foods_trip ON foods (trip)",
    "CREATE UNIQUE INDEX idx_foods_notion_id ON foods (notion_id) WHERE notion_id != ''",
  ];
  c.indexes = [
    ...existing,
    ...toAdd.filter((s) => !existing.some((e) =>
      e.includes(s.match(/idx_foods_\w+/)[0])
    )),
  ];

  app.save(c);
}, (app) => {
  const c = app.findCollectionByNameOrId("foods");
  for (const name of ["stop", "day", "trip", "notion_id", "notion_last_edited", "last_synced_at"]) {
    const f = c.fields.getByName(name);
    if (f) c.fields.removeById(f.id);
  }
  const drop = ["idx_foods_stop", "idx_foods_day", "idx_foods_trip", "idx_foods_notion_id"];
  c.indexes = (c.indexes || []).filter((s) => !drop.some((n) => s.includes(n)));
  app.save(c);
});
