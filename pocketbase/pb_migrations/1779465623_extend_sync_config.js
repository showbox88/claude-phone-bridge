/// <reference path="../pb_data/types.d.ts" />
//
// Extend sync_config with the three columns that previously lived as
// hardcoded Python dicts. After this migration, runner.py /
// reconcile_initial.py / pb_tools.py read these values from the
// sync_config rows instead of their local maps.
//
// Idempotent: skips each field if already present; seeds existing rows
// only when the new column is null / empty.
//
migrate((app) => {
  const c = app.findCollectionByNameOrId("sync_config");

  if (!c.fields.getByName("title_field")) {
    c.fields.add(new Field({ name: "title_field", type: "text", required: true, max: 60 }));
    app.save(c);
  }
  if (!c.fields.getByName("date_field")) {
    c.fields.add(new Field({ name: "date_field", type: "text", max: 60 }));
    app.save(c);
  }
  if (!c.fields.getByName("auto_sync")) {
    c.fields.add(new Field({ name: "auto_sync", type: "bool" }));
    app.save(c);
  }

  const SEED = {
    trips:     { title_field: "title", date_field: "date_start",  auto_sync: true  },
    plans:     { title_field: "title", date_field: "target_date", auto_sync: false },
    todos:     { title_field: "title", date_field: "due_date",    auto_sync: true  },
    journal:   { title_field: "title", date_field: "date",        auto_sync: true  },
    days:      { title_field: "name",  date_field: "date",        auto_sync: true  },
    contacts:  { title_field: "name",  date_field: "",            auto_sync: false },
    locations: { title_field: "name",  date_field: "",            auto_sync: true  },
    stops:     { title_field: "name",  date_field: "date",        auto_sync: true  },
  };
  const rows = app.findRecordsByFilter("sync_config", "");
  for (const row of rows) {
    const seed = SEED[row.get("collection")];
    if (!seed) continue;
    let dirty = false;
    if (!row.get("title_field")) { row.set("title_field", seed.title_field); dirty = true; }
    if (row.get("date_field") == null || row.get("date_field") === "") {
      row.set("date_field", seed.date_field); dirty = true;
    }
    if (row.get("auto_sync") === null || row.get("auto_sync") === undefined) {
      row.set("auto_sync", seed.auto_sync); dirty = true;
    }
    if (dirty) app.save(row);
  }
}, (app) => {
  const c = app.findCollectionByNameOrId("sync_config");
  for (const name of ["title_field", "date_field", "auto_sync"]) {
    const f = c.fields.getByName(name);
    if (f) { c.fields.removeById(f.id); app.save(c); }
  }
});
