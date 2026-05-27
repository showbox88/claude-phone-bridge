/// <reference path="../pb_data/types.d.ts" />
migrate((app) => {
  const locations = app.findCollectionByNameOrId("locations");

  const collection = new Collection({
    name: "foods",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "dish", type: "text", required: true, max: 500 },
      {
        name: "currency",
        type: "select",
        maxSelect: 1,
        values: ["JPY", "EUR", "USD", "CNY", "其他"],
      },
      { name: "price", type: "number" },
      {
        name: "flavor",
        type: "select",
        maxSelect: 6,
        values: ["辣", "甜", "咸", "酸", "清淡", "油腻"],
      },
      {
        name: "rating",
        type: "select",
        maxSelect: 1,
        values: ["❤️", "❤️❤️", "❤️❤️❤️", "❤️❤️❤️❤️", "❤️❤️❤️❤️❤️"],
      },
      { name: "want_again", type: "bool" },
      { name: "location", type: "relation", collectionId: locations.id, maxSelect: 1, cascadeDelete: false },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_foods_location ON foods (location)",
      "CREATE INDEX idx_foods_rating ON foods (rating)",
    ],
  });
  app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("foods");
  app.delete(collection);
});
