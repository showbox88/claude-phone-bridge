/// <reference path="../pb_data/types.d.ts" />
//
// Wire `trips.related_plan` → plans and `trips.companions` → contacts (multi).
// Done in a follow-up migration because plans and contacts didn't exist when
// trips was first created (1779465601). Notion's reverse rollups
// ("Related to Trips (Companions)" on contacts, "Related to Trips (Related Plan)"
// on plans) come for free from these relations on query.
//
migrate((app) => {
  const trips    = app.findCollectionByNameOrId("trips");
  const plans    = app.findCollectionByNameOrId("plans");
  const contacts = app.findCollectionByNameOrId("contacts");

  trips.fields.add(new Field({
    name: "related_plan",
    type: "relation",
    collectionId: plans.id,
    maxSelect: 1,
    cascadeDelete: false,
  }));
  trips.fields.add(new Field({
    name: "companions",
    type: "relation",
    collectionId: contacts.id,
    maxSelect: 999,
    cascadeDelete: false,
  }));
  trips.indexes = [...(trips.indexes || []),
    "CREATE INDEX idx_trips_related_plan ON trips (related_plan)",
  ];
  app.save(trips);
}, (app) => {
  const trips = app.findCollectionByNameOrId("trips");
  for (const name of ["related_plan", "companions"]) {
    const f = trips.fields.getByName(name);
    if (f) trips.fields.removeById(f.id);
  }
  trips.indexes = (trips.indexes || []).filter((i) => !i.includes("idx_trips_related_plan"));
  app.save(trips);
});
