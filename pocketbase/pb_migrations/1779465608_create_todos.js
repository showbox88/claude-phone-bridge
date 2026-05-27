/// <reference path="../pb_data/types.d.ts" />
//
// Todos — the work queue. Executor + Executor Ref ID let an external app
// (Google Calendar / Google Tasks) own the actual reminder; this row is the
// canonical Smart Note record. Mirrors Notion `Todos` 1:1.
//
migrate((app) => {
  const collection = new Collection({
    name: "todos",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "title", type: "text", required: true, max: 500 },
      { name: "due_date", type: "date" },
      { name: "completed_at", type: "date" },
      {
        name: "priority",
        type: "select",
        maxSelect: 1,
        values: ["Low", "Normal", "High"],
      },
      {
        name: "status",
        type: "select",
        maxSelect: 1,
        values: ["Pending", "Done", "Cancelled"],
      },
      {
        name: "executor",
        type: "select",
        maxSelect: 1,
        values: ["none", "gcal", "gtask", "other"],
      },
      { name: "executor_ref_id", type: "text" },
      {
        name: "tags",
        type: "select",
        maxSelect: 5,
        values: ["工作", "家人", "学习", "生活", "重要"],
      },
      { name: "content", type: "editor" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_todos_status ON todos (status)",
      "CREATE INDEX idx_todos_due_date ON todos (due_date)",
      "CREATE INDEX idx_todos_priority ON todos (priority)",
    ],
  });
  app.save(collection);
}, (app) => {
  const c = app.findCollectionByNameOrId("todos");
  app.delete(c);
});
