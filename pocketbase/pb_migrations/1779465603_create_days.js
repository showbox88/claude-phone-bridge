/// <reference path="../pb_data/types.d.ts" />
migrate((app) => {
  const trips = app.findCollectionByNameOrId("trips");
  const locations = app.findCollectionByNameOrId("locations");

  const collection = new Collection({
    name: "days",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "name", type: "text", required: true, max: 500 },
      { name: "date", type: "date" },
      { name: "reserved", type: "date" },
      { name: "checkin", type: "date" },
      { name: "amount", type: "number" },
      {
        name: "currency",
        type: "select",
        maxSelect: 1,
        values: ["JPY", "EUR", "USD", "CNY", "其他"],
      },
      { name: "rate", type: "number" },
      { name: "amount_usd", type: "number" },
      {
        name: "activity_type",
        type: "select",
        maxSelect: 1,
        values: ["景点观光", "爬山/徒步", "用餐", "购物", "休息", "交通", "娱乐", "其他"],
      },
      { name: "score", type: "number", min: 0, max: 10 },
      { name: "note", type: "text" },
      { name: "trip", type: "relation", collectionId: trips.id, maxSelect: 1, cascadeDelete: false },
      { name: "location", type: "relation", collectionId: locations.id, maxSelect: 1, cascadeDelete: false },
      { name: "actual_lat", type: "number" },
      { name: "actual_lng", type: "number" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_days_date ON days (date)",
      "CREATE INDEX idx_days_trip ON days (trip)",
      "CREATE INDEX idx_days_location ON days (location)",
      "CREATE INDEX idx_days_activity_type ON days (activity_type)",
    ],
  });
  app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("days");
  app.delete(collection);
});
