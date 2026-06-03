/// <reference path="../pb_data/types.d.ts" />
//
// Create `stops` — the atomic event under a day. Lifts 11 fields out of
// the original atomic `days` model so days becomes a pure container.
// See docs/superpowers/specs/2026-06-03-stops-redesign-design.md.
//
// Stops is the 7th sync target. Pipeline fields (notion_id,
// notion_last_edited, last_synced_at) are added inline here rather than
// via the pattern in 1779465617 so we get them in one shot.
//
// Relations are PB-side only this round (transform.py:40-46 / :70-72
// skips relation types in both sync directions). Future PR adds
// PB-id ↔ Notion-page-id translation.
//
migrate((app) => {
  const days      = app.findCollectionByNameOrId("days");
  const trips     = app.findCollectionByNameOrId("trips");
  const locations = app.findCollectionByNameOrId("locations");
  const contacts  = app.findCollectionByNameOrId("contacts");
  const journal   = app.findCollectionByNameOrId("journal");

  const collection = new Collection({
    name: "stops",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "name", type: "text", required: true, max: 500 },
      { name: "date", type: "date" },
      { name: "reserved", type: "date" },
      { name: "checkin", type: "date" },
      {
        name: "categories",
        type: "select",
        maxSelect: 8,
        values: ["打卡", "酒店", "餐厅", "购物", "体验", "交通", "笔记", "消费"],
      },
      { name: "amount", type: "number" },
      {
        name: "currency",
        type: "select",
        maxSelect: 1,
        values: ["JPY", "EUR", "USD", "CNY", "其他"],
      },
      { name: "rate", type: "number" },
      { name: "amount_usd", type: "number" },
      { name: "note", type: "text" },
      { name: "actual_lat", type: "number" },
      { name: "actual_lng", type: "number" },

      // relations — PB-only this round
      { name: "day",      type: "relation", collectionId: days.id,      maxSelect: 1, cascadeDelete: false },
      { name: "trip",     type: "relation", collectionId: trips.id,     maxSelect: 1, cascadeDelete: false },
      { name: "location", type: "relation", collectionId: locations.id, maxSelect: 1, cascadeDelete: false },
      { name: "contact",  type: "relation", collectionId: contacts.id,  maxSelect: 1, cascadeDelete: false },
      { name: "journal",  type: "relation", collectionId: journal.id,   maxSelect: 1, cascadeDelete: false },

      // sync pipeline
      { name: "notion_id",          type: "text", max: 100 },
      { name: "notion_last_edited", type: "date" },
      { name: "last_synced_at",     type: "date" },

      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_stops_date     ON stops (date)",
      "CREATE INDEX idx_stops_day      ON stops (day)",
      "CREATE INDEX idx_stops_trip     ON stops (trip)",
      "CREATE INDEX idx_stops_location ON stops (location)",
      "CREATE INDEX idx_stops_contact  ON stops (contact)",
      "CREATE UNIQUE INDEX idx_stops_notion_id ON stops (notion_id) WHERE notion_id != ''",
    ],
  });
  app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("stops");
  app.delete(collection);
});
