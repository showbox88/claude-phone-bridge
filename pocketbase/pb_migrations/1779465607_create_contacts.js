/// <reference path="../pb_data/types.d.ts" />
//
// Contacts — people in Ryan's address book. Mirrors Notion `Contacts` 1:1.
// Trip.companions will reference this later (1779465615_link_trips...).
//
migrate((app) => {
  const collection = new Collection({
    name: "contacts",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "name", type: "text", required: true, max: 200 },
      { name: "company", type: "text" },
      { name: "city", type: "text" },
      { name: "email", type: "email" },
      { name: "phone", type: "text" },
      { name: "birthday", type: "date" },
      { name: "last_contact", type: "date" },
      {
        name: "relationship",
        type: "select",
        maxSelect: 1,
        values: ["家人", "朋友", "同事", "客户", "其他"],
      },
      {
        name: "tags",
        type: "select",
        maxSelect: 3,
        values: ["重要", "工作", "家人"],
      },
      { name: "content", type: "editor" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_contacts_relationship ON contacts (relationship)",
      "CREATE INDEX idx_contacts_birthday ON contacts (birthday)",
    ],
  });
  app.save(collection);
}, (app) => {
  const c = app.findCollectionByNameOrId("contacts");
  app.delete(c);
});
