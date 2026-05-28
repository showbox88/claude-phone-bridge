/// <reference path="../pb_data/types.d.ts" />
//
// Daily Briefing — generated digests Smart Note can refer back to.
// The body content (the actual briefing prose) lives in `content`.
// Items counts are denormalized snapshots from when the briefing was made.
//
migrate((app) => {
  const collection = new Collection({
    name: "daily_briefing",
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
        name: "type",
        type: "select",
        maxSelect: 1,
        values: ["Morning", "Noon", "Evening"],
      },
      {
        name: "status",
        type: "select",
        maxSelect: 1,
        values: ["Generated", "Reviewed", "Archived"],
      },
      { name: "items_pending_count", type: "number" },
      { name: "items_completed_today", type: "number" },
      { name: "family_events_flagged", type: "number" },
      { name: "content", type: "editor" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_briefing_date ON daily_briefing (date)",
      "CREATE INDEX idx_briefing_status ON daily_briefing (status)",
    ],
  });
  app.save(collection);
}, (app) => {
  const c = app.findCollectionByNameOrId("daily_briefing");
  app.delete(c);
});
