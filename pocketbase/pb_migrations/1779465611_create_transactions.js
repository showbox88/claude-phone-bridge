/// <reference path="../pb_data/types.d.ts" />
//
// Transactions — credit-card-level ledger (USD). Source can be manual or a
// Gmail receipt scraper. `confirmation` is the dedup key when ingesting from
// email so a re-scan doesn't double-insert.
//
migrate((app) => {
  const collection = new Collection({
    name: "transactions",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "description", type: "text", required: true, max: 500 },
      { name: "amount", type: "number" },
      { name: "date", type: "date" },
      {
        name: "type",
        type: "select",
        maxSelect: 1,
        values: ["支出", "退款"],
      },
      {
        name: "category",
        type: "select",
        maxSelect: 1,
        values: ["旅行", "订阅服务", "娱乐", "交通", "购物/日用", "餐饮"],
      },
      {
        name: "card",
        type: "select",
        maxSelect: 1,
        values: ["Chase Sapphire Preferred (7675)"],
      },
      { name: "confirmation", type: "text" },
      {
        name: "source",
        type: "select",
        maxSelect: 1,
        values: ["手动", "Gmail"],
      },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_tx_date ON transactions (date)",
      "CREATE INDEX idx_tx_category ON transactions (category)",
      "CREATE UNIQUE INDEX idx_tx_confirmation ON transactions (confirmation) WHERE confirmation != ''",
    ],
  });
  app.save(collection);
}, (app) => {
  const c = app.findCollectionByNameOrId("transactions");
  app.delete(c);
});
