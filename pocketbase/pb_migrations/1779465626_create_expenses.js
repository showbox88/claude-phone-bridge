/// <reference path="../pb_data/types.d.ts" />
//
// Create `expenses` — the new child-of-stops money table. Replaces the
// `transactions` collection. transactions is left alone here; a separate
// Python script copies rows over, then a later migration drops transactions.
//
// See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md.
//
migrate((app) => {
  const stops = app.findCollectionByNameOrId("stops");
  const days  = app.findCollectionByNameOrId("days");
  const trips = app.findCollectionByNameOrId("trips");

  const collection = new Collection({
    name: "expenses",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "description",      type: "text", required: true, max: 500 },
      { name: "amount",           type: "number" },
      {
        name: "currency",
        type: "select",
        maxSelect: 1,
        values: ["USD", "JPY", "EUR", "CNY", "其他"],
      },
      { name: "rate",             type: "number" },
      { name: "amount_usd",       type: "number" },
      { name: "date",             type: "date" },
      {
        name: "type",
        type: "select",
        maxSelect: 1,
        values: ["支出", "退款"],
      },
      {
        name: "expense_category",
        type: "select",
        maxSelect: 1,
        values: [
          "旅行", "订阅服务", "娱乐", "交通", "购物/日用",
          "餐饮", "门票", "住宿", "代付", "其他",
        ],
      },
      {
        name: "card",
        type: "select",
        maxSelect: 1,
        values: ["Chase Sapphire Preferred (7675)"],
      },
      { name: "confirmation",     type: "text" },
      {
        name: "source",
        type: "select",
        maxSelect: 1,
        values: ["手动", "Gmail", "Agent"],
      },

      // relations — PB side only this round (sync ignores relations)
      { name: "stop", type: "relation", collectionId: stops.id, maxSelect: 1, cascadeDelete: false },
      { name: "day",  type: "relation", collectionId: days.id,  maxSelect: 1, cascadeDelete: false },
      { name: "trip", type: "relation", collectionId: trips.id, maxSelect: 1, cascadeDelete: false },

      // sync pipeline (for PR2; harmless if PR2 not shipped)
      { name: "notion_id",          type: "text", max: 100 },
      { name: "notion_last_edited", type: "date" },
      { name: "last_synced_at",     type: "date" },

      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_expenses_date     ON expenses (date)",
      "CREATE INDEX idx_expenses_category ON expenses (expense_category)",
      "CREATE INDEX idx_expenses_stop     ON expenses (stop)",
      "CREATE INDEX idx_expenses_day      ON expenses (day)",
      "CREATE INDEX idx_expenses_trip     ON expenses (trip)",
      "CREATE UNIQUE INDEX idx_expenses_confirmation ON expenses (confirmation) WHERE confirmation != ''",
      "CREATE UNIQUE INDEX idx_expenses_notion_id    ON expenses (notion_id)    WHERE notion_id != ''",
    ],
  });
  app.save(collection);
}, (app) => {
  const c = app.findCollectionByNameOrId("expenses");
  app.delete(c);
});
