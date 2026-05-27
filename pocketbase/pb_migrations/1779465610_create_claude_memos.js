/// <reference path="../pb_data/types.d.ts" />
//
// Claude Memos — Ryan's running design-decision archive. Body content
// (architecture notes, decisions, scratchpad) is the primary payload.
//
migrate((app) => {
  const collection = new Collection({
    name: "claude_memos",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "title", type: "text", required: true, max: 500 },
      { name: "date", type: "date" },
      {
        name: "category",
        type: "select",
        maxSelect: 1,
        values: ["偏好约定", "项目状态", "决策结论", "待办线索", "技术细节", "其他"],
      },
      {
        name: "priority",
        type: "select",
        maxSelect: 1,
        values: ["High", "Low"],
      },
      {
        name: "status",
        type: "select",
        maxSelect: 1,
        values: ["Active", "Archived"],
      },
      { name: "content", type: "editor" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_memos_category ON claude_memos (category)",
      "CREATE INDEX idx_memos_status ON claude_memos (status)",
      "CREATE INDEX idx_memos_date ON claude_memos (date)",
    ],
  });
  app.save(collection);
}, (app) => {
  const c = app.findCollectionByNameOrId("claude_memos");
  app.delete(c);
});
