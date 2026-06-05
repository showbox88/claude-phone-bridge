/// <reference path="../pb_data/types.d.ts" />
//
// Drop the legacy `transactions` collection. Safety check: every
// transaction row must already have a counterpart in expenses (by
// confirmation OR by date+description+amount). If anything looks
// unmigrated, throw and bail.
//
// Run scripts/migrate_transactions_to_expenses.py BEFORE deploying this.
//
// See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md.
//
migrate((app) => {
  let tx;
  try {
    tx = app.findCollectionByNameOrId("transactions");
  } catch (e) {
    return;
  }

  const txRows = app.findRecordsByFilter("transactions", "", "", 1000, 0);
  const unmigrated = [];
  for (const t of txRows) {
    const conf = (t.get("confirmation") || "").trim();
    let matches = [];
    if (conf) {
      matches = app.findRecordsByFilter(
        "expenses", `confirmation = "${conf}"`, "", 1, 0
      );
    } else {
      const date = String(t.get("date") || "").substring(0, 10);
      const desc = String(t.get("description") || "").replace(/"/g, '\\"');
      const amount = t.get("amount") || 0;
      matches = app.findRecordsByFilter(
        "expenses",
        `date >= "${date} 00:00:00" && date < "${date} 23:59:59" && description = "${desc}" && amount = ${amount}`,
        "", 1, 0
      );
    }
    if (matches.length === 0) {
      unmigrated.push(t.id);
    }
  }
  if (unmigrated.length > 0) {
    throw new Error(
      "Refusing to drop transactions: " + unmigrated.length +
      " row(s) not yet migrated to expenses. Run " +
      "scripts/migrate_transactions_to_expenses.py first. Sample ids: " +
      unmigrated.slice(0, 3).join(", ")
    );
  }

  app.delete(tx);
}, (app) => {
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
      { name: "amount",      type: "number" },
      { name: "date",        type: "date" },
      { name: "type",        type: "select", maxSelect: 1, values: ["支出", "退款"] },
      { name: "category",    type: "select", maxSelect: 1,
        values: ["旅行", "订阅服务", "娱乐", "交通", "购物/日用", "餐饮"] },
      { name: "card",        type: "select", maxSelect: 1,
        values: ["Chase Sapphire Preferred (7675)"] },
      { name: "confirmation", type: "text" },
      { name: "source",      type: "select", maxSelect: 1, values: ["手动", "Gmail"] },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_tx_date     ON transactions (date)",
      "CREATE INDEX idx_tx_category ON transactions (category)",
      "CREATE UNIQUE INDEX idx_tx_confirmation ON transactions (confirmation) WHERE confirmation != ''",
    ],
  });
  app.save(collection);
});
