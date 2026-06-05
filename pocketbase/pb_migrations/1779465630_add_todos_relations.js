/// <reference path="../pb_data/types.d.ts" />
//
// Add `stop` / `day` / `trip` single-relation optional fields to `todos`.
// Same shape as expenses — lets a todo hang under a specific stop
// (e.g. "call ahead before visiting the temple"), a calendar day
// (e.g. "Day 1 pack list"), or a whole trip (e.g. "pre-trip prep").
//
// Convention: writer-side keeps `todo.trip == todo.day.trip` when day
// is set, matching the expense/stop pattern.
//
// See docs/data-model.md.
//
migrate((app) => {
  const c     = app.findCollectionByNameOrId("todos");
  const stops = app.findCollectionByNameOrId("stops");
  const days  = app.findCollectionByNameOrId("days");
  const trips = app.findCollectionByNameOrId("trips");

  const specs = [
    { name: "stop", collectionId: stops.id },
    { name: "day",  collectionId: days.id },
    { name: "trip", collectionId: trips.id },
  ];
  for (const spec of specs) {
    if (c.fields.getByName(spec.name)) continue;
    c.fields.add(new Field({
      name:          spec.name,
      type:          "relation",
      collectionId:  spec.collectionId,
      maxSelect:     1,
      cascadeDelete: false,
    }));
  }

  const idxToAdd = [
    "CREATE INDEX idx_todos_stop ON todos (stop)",
    "CREATE INDEX idx_todos_day  ON todos (day)",
    "CREATE INDEX idx_todos_trip ON todos (trip)",
  ];
  const existing = (c.indexes || []);
  c.indexes = [
    ...existing,
    ...idxToAdd.filter((s) => !existing.some((e) => e.includes(s.match(/idx_todos_\w+/)[0]))),
  ];

  app.save(c);
}, (app) => {
  const c = app.findCollectionByNameOrId("todos");
  for (const name of ["stop", "day", "trip"]) {
    const f = c.fields.getByName(name);
    if (f) c.fields.removeById(f.id);
  }
  c.indexes = (c.indexes || []).filter((s) =>
    !["idx_todos_stop", "idx_todos_day", "idx_todos_trip"].some((n) => s.includes(n))
  );
  app.save(c);
});
