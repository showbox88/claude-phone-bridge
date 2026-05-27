/// <reference path="../pb_data/types.d.ts" />
//
// Pages — standalone Notion pages under "Smart Note" (anything that's not a
// row inside one of the 12 databases). Examples: Ryan 个人 Profile, the
// long-term roadmap page, any nested sub-page Ryan drops in.
//
// Hierarchy is preserved via `parent` (self-relation). `notion_id` is the
// original page ID so an eventual one-way sync back to Notion can find the
// matching record.
//
migrate((app) => {
  const collection = new Collection({
    name: "pages",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "title", type: "text", required: true, max: 500 },
      { name: "icon", type: "text" },           // emoji from Notion (e.g. 👤)
      { name: "content", type: "editor" },      // page body, Markdown-formatted
      { name: "notion_id", type: "text" },      // Notion page UUID for resync
      { name: "notion_url", type: "url" },      // direct Notion link
      {
        name: "tags",
        type: "select",
        maxSelect: 10,
        values: ["笔记", "方法论", "Profile", "路线图", "Dashboard", "参考", "草稿", "工作", "学习", "生活"],
      },
      { name: "archived", type: "bool" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE INDEX idx_pages_archived ON pages (archived)",
      // Notion page IDs are unique per workspace; enforce so a re-import can't dup.
      "CREATE UNIQUE INDEX idx_pages_notion_id ON pages (notion_id) WHERE notion_id != ''",
    ],
  });
  app.save(collection);

  // Self-relation: parent → pages (second save after collection has an ID).
  const pages = app.findCollectionByNameOrId("pages");
  pages.fields.add(new Field({
    name: "parent",
    type: "relation",
    collectionId: pages.id,
    maxSelect: 1,
    cascadeDelete: false,
  }));
  pages.indexes = [...(pages.indexes || []),
    "CREATE INDEX idx_pages_parent ON pages (parent)",
  ];
  app.save(pages);
}, (app) => {
  const c = app.findCollectionByNameOrId("pages");
  app.delete(c);
});
