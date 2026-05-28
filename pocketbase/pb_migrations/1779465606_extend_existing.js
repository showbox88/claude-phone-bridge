/// <reference path="../pb_data/types.d.ts" />
//
// Add Notion-parity fields to the original 5 collections:
//   • content (editor) on trips/locations/days/foods → preserves Notion page-body
//     text that holds Bookings, free-form notes, etc.
//   • fsq_id (text, unique) on locations → lets Foursquare-sourced POIs dedupe
//     by ID alongside the existing osm_id / amap_poi_id paths.
//
migrate((app) => {
  for (const name of ["trips", "locations", "days", "foods"]) {
    const c = app.findCollectionByNameOrId(name);
    c.fields.add(new Field({ name: "content", type: "editor" }));
    app.save(c);
  }
  const locs = app.findCollectionByNameOrId("locations");
  locs.fields.add(new Field({ name: "fsq_id", type: "text" }));
  locs.indexes = [...(locs.indexes || []),
    "CREATE UNIQUE INDEX idx_locations_fsq_id ON locations (fsq_id) WHERE fsq_id != ''",
  ];
  app.save(locs);
}, (app) => {
  for (const name of ["trips", "locations", "days", "foods"]) {
    const c = app.findCollectionByNameOrId(name);
    const f = c.fields.getByName("content");
    if (f) c.fields.removeById(f.id);
    app.save(c);
  }
  const locs = app.findCollectionByNameOrId("locations");
  const fsq = locs.fields.getByName("fsq_id");
  if (fsq) locs.fields.removeById(fsq.id);
  locs.indexes = (locs.indexes || []).filter((i) => !i.includes("idx_locations_fsq_id"));
  app.save(locs);
});
