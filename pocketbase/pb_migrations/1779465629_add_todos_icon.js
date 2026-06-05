/// <reference path="../pb_data/types.d.ts" />
//
// Add `icon` text field to `todos`. Stores the emoji that becomes the
// Notion page icon. PB doesn't have a native page-icon mechanism — this
// is just data the sync layer reads via icon_for_todo().
//
// See docs/data-model.md and notion_sync/icons.py.
//
migrate((app) => {
  const c = app.findCollectionByNameOrId("todos");
  if (c.fields.getByName("icon")) return;
  c.fields.add(new Field({
    name: "icon",
    type: "text",
    max: 10,
  }));
  app.save(c);
}, (app) => {
  const c = app.findCollectionByNameOrId("todos");
  const f = c.fields.getByName("icon");
  if (f) {
    c.fields.removeById(f.id);
    app.save(c);
  }
});
