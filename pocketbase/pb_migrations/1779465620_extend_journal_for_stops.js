/// <reference path="../pb_data/types.d.ts" />
//
// Wire `journal` into the stops redesign:
//   • Add related_stop relation → stops (parallel to existing
//     related_trip / related_day).
//   • Append "Reminder" to journal.type's select values. English value
//     for parity with existing Learning/Feeling/Observation/Event/Diary;
//     UI / agent surfaces it as "注意" in Chinese contexts (display-only).
//   • Add sync pipeline fields (notion_id / notion_last_edited /
//     last_synced_at) + the matching unique index. Journal becomes
//     the 8th sync target.
//
// See docs/superpowers/specs/2026-06-03-stops-redesign-design.md §2.3 + §6 Phase 1.
//
migrate((app) => {
  const stops   = app.findCollectionByNameOrId("stops");
  const journal = app.findCollectionByNameOrId("journal");

  if (!journal.fields.getByName("related_stop")) {
    journal.fields.add(new Field({
      name: "related_stop",
      type: "relation",
      collectionId: stops.id,
      maxSelect: 1,
      cascadeDelete: false,
    }));
  }

  const typeField = journal.fields.getByName("type");
  if (typeField && !typeField.values.includes("Reminder")) {
    typeField.values = [...typeField.values, "Reminder"];
  }

  for (const spec of [
    { name: "notion_id",          type: "text", max: 100 },
    { name: "notion_last_edited", type: "date" },
    { name: "last_synced_at",     type: "date" },
  ]) {
    if (!journal.fields.getByName(spec.name)) {
      journal.fields.add(new Field(spec));
    }
  }

  const idxName = "idx_journal_notion_id";
  const indexes = [...(journal.indexes || [])];
  if (!indexes.some((s) => s.includes(idxName))) {
    journal.indexes = [...indexes,
      `CREATE UNIQUE INDEX ${idxName} ON journal (notion_id) WHERE notion_id != ''`,
    ];
  }

  app.save(journal);
}, (app) => {
  const journal = app.findCollectionByNameOrId("journal");

  for (const name of ["related_stop", "notion_id", "notion_last_edited", "last_synced_at"]) {
    const f = journal.fields.getByName(name);
    if (f) journal.fields.removeById(f.id);
  }

  const typeField = journal.fields.getByName("type");
  if (typeField) {
    typeField.values = typeField.values.filter((v) => v !== "Reminder");
  }

  const idxName = "idx_journal_notion_id";
  journal.indexes = (journal.indexes || []).filter((s) => !s.includes(idxName));

  app.save(journal);
});
