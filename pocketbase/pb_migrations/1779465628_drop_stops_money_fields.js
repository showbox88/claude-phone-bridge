/// <reference path="../pb_data/types.d.ts" />
//
// Drop amount / currency / rate / amount_usd from stops. Safety check:
// every stop with amount > 0 must have ≥1 expense row pointing at it. If
// anything looks unmigrated, throw and bail.
//
// Run scripts/migrate_stops_money_to_expenses.py BEFORE deploying this.
//
// See docs/superpowers/specs/2026-06-05-expenses-redesign-design.md.
//
migrate((app) => {
  const moneyStops = app.findRecordsByFilter("stops", "amount > 0", "", 10000, 0);
  const unmigrated = [];
  for (const s of moneyStops) {
    const matches = app.findRecordsByFilter("expenses", `stop = "${s.id}"`, "", 1, 0);
    if (matches.length === 0) {
      unmigrated.push(s.id);
    }
  }
  if (unmigrated.length > 0) {
    throw new Error(
      "Refusing to drop stops money fields: " + unmigrated.length +
      " stop(s) with amount > 0 have no linked expense. Run " +
      "scripts/migrate_stops_money_to_expenses.py first. Sample ids: " +
      unmigrated.slice(0, 3).join(", ")
    );
  }

  const c = app.findCollectionByNameOrId("stops");
  for (const name of ["amount", "currency", "rate", "amount_usd"]) {
    const f = c.fields.getByName(name);
    if (f) c.fields.removeById(f.id);
  }
  app.save(c);
}, (app) => {
  const c = app.findCollectionByNameOrId("stops");
  const specs = [
    { name: "amount",     type: "number" },
    { name: "currency",   type: "select", maxSelect: 1, values: ["JPY", "EUR", "USD", "CNY", "其他"] },
    { name: "rate",       type: "number" },
    { name: "amount_usd", type: "number" },
  ];
  for (const spec of specs) {
    if (!c.fields.getByName(spec.name)) {
      c.fields.add(new Field(spec));
    }
  }
  app.save(c);
});
