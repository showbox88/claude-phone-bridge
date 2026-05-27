/// <reference path="../pb_data/types.d.ts" />
migrate((app) => {
  const collection = new Collection({
    name: "trips",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "title", type: "text", required: true, max: 500 },
      { name: "date_start", type: "date" },
      { name: "date_end", type: "date" },
      { name: "origin", type: "text" },
      { name: "destination", type: "text" },
      { name: "budget", type: "number" },
      {
        name: "status",
        type: "select",
        maxSelect: 1,
        values: ["Planning", "Booked", "Ongoing", "Done", "Cancelled"],
      },
      {
        name: "type",
        type: "select",
        maxSelect: 1,
        values: ["Leisure", "Business", "Family", "Other"],
      },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_trips_date_start ON trips (date_start)",
      "CREATE INDEX idx_trips_date_end ON trips (date_end)",
      "CREATE INDEX idx_trips_status ON trips (status)",
    ],
  });
  app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("trips");
  app.delete(collection);
});
