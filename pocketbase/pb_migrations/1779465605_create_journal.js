/// <reference path="../pb_data/types.d.ts" />
migrate((app) => {
  const trips = app.findCollectionByNameOrId("trips");
  const days = app.findCollectionByNameOrId("days");

  const collection = new Collection({
    name: "journal",
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
        name: "mood",
        type: "select",
        maxSelect: 1,
        values: ["Happy", "Sad", "Anxious", "Excited", "Calm", "Frustrated", "Grateful", "Reflective", "Energized"],
      },
      {
        name: "type",
        type: "select",
        maxSelect: 1,
        values: ["Learning", "Feeling", "Observation", "Event", "Diary"],
      },
      {
        name: "tags",
        type: "select",
        maxSelect: 5,
        values: ["工作", "家人", "学习", "读书", "生活"],
      },
      { name: "content", type: "editor" },
      { name: "related_trip", type: "relation", collectionId: trips.id, maxSelect: 1, cascadeDelete: false },
      { name: "related_day", type: "relation", collectionId: days.id, maxSelect: 1, cascadeDelete: false },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_journal_date ON journal (date)",
      "CREATE INDEX idx_journal_mood ON journal (mood)",
    ],
  });
  app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("journal");
  app.delete(collection);
});
