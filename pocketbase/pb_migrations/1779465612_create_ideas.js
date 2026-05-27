/// <reference path="../pb_data/types.d.ts" />
//
// Ideas — the seed pool. Self-relation `related_ideas` is added in a second
// save() after the collection's own ID exists. Notion's "Linked from" is the
// reverse direction of `related_ideas` and is computed on demand in queries,
// not stored.
//
migrate((app) => {
  const collection = new Collection({
    name: "ideas",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "title", type: "text", required: true, max: 500 },
      {
        name: "category",
        type: "select",
        maxSelect: 1,
        values: ["Work", "Personal", "Creative", "Technical", "Other"],
      },
      {
        name: "status",
        type: "select",
        maxSelect: 1,
        values: ["Seedling", "Growing", "Mature", "Archived"],
      },
      {
        name: "tags",
        type: "select",
        maxSelect: 5,
        values: ["工作", "家人", "学习", "灵感", "重要"],
      },
      { name: "connection_notes", type: "text" },
      { name: "conversation_count", type: "number" },
      { name: "content", type: "editor" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_ideas_status ON ideas (status)",
      "CREATE INDEX idx_ideas_category ON ideas (category)",
    ],
  });
  app.save(collection);

  // Self-relation: second save after the collection has an ID.
  const ideas = app.findCollectionByNameOrId("ideas");
  ideas.fields.add(new Field({
    name: "related_ideas",
    type: "relation",
    collectionId: ideas.id,
    maxSelect: 999,
    cascadeDelete: false,
  }));
  app.save(ideas);
}, (app) => {
  const c = app.findCollectionByNameOrId("ideas");
  app.delete(c);
});
