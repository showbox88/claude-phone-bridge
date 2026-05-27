/// <reference path="../pb_data/types.d.ts" />
//
// Plans — multi-step intentions. References ideas (must exist already; created
// by 1779465612). The reverse "Plans linking here" rollup in Notion's ideas
// schema is computed on the fly from this relation, not stored.
//
migrate((app) => {
  const ideas = app.findCollectionByNameOrId("ideas");

  const collection = new Collection({
    name: "plans",
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
        values: ["Work", "Learning", "Health", "Personal", "Financial"],
      },
      {
        name: "status",
        type: "select",
        maxSelect: 1,
        values: ["Active", "Paused", "Done", "Abandoned"],
      },
      { name: "progress", type: "number", min: 0, max: 100 },
      { name: "target_date", type: "date" },
      { name: "last_update", type: "date" },
      {
        name: "related_ideas",
        type: "relation",
        collectionId: ideas.id,
        maxSelect: 999,
        cascadeDelete: false,
      },
      { name: "content", type: "editor" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_plans_status ON plans (status)",
      "CREATE INDEX idx_plans_category ON plans (category)",
      "CREATE INDEX idx_plans_target_date ON plans (target_date)",
    ],
  });
  app.save(collection);
}, (app) => {
  const c = app.findCollectionByNameOrId("plans");
  app.delete(c);
});
