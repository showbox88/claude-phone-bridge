"""Tests for notion_sync.provisioner (no real PB / Notion calls)."""
import pytest
from notion_sync import provisioner


class FakePB:
    """Stand-in for PBClient.

    `_http` returns the canned collection dict; `list_records` is used by
    notion_sync.config.load_all (we pre-populate sync_config rows).
    """
    def __init__(self, collections, sync_config_rows):
        self.collections = collections           # name -> coll dict
        self.sync_rows = sync_config_rows

    def list_records(self, name, *_, **__):
        if name == "sync_config":
            return list(self.sync_rows)
        return []

    def _http(self, method, path, body=None):    # noqa: ARG002
        if method == "GET" and path.startswith("/api/collections/"):
            name_or_id = path.rsplit("/", 1)[-1]
            for c in self.collections.values():
                if c["name"] == name_or_id or c["id"] == name_or_id:
                    return c
            raise RuntimeError(f"collection not found: {name_or_id}")
        if method == "PATCH" and path.startswith("/api/collections/"):
            name = path.rsplit("/", 1)[-1]
            if name not in self.collections:
                raise RuntimeError(f"collection not found: {name}")
            # Apply the patch in-place so subsequent GETs see the update.
            if "fields" in body:
                self.collections[name]["fields"] = body["fields"]
            if "indexes" in body:
                self.collections[name]["indexes"] = body["indexes"]
            return self.collections[name]
        raise NotImplementedError(method, path)


class FakeNotion:
    def __init__(self):
        self.created_dbs = []
        self.patched_dbs = []
        self.activity_db = {
            "properties": {"collection": {"select": {"options": [
                {"name": "trips"},
            ]}}}
        }
    def create_database(self, parent_page_id, title, properties):
        db = {"id": "new-db-uuid", "title": title, "properties": properties}
        self.created_dbs.append(db)
        return db
    def retrieve_database(self, db_id):
        return self.activity_db
    def update_database(self, db_id, body):
        self.patched_dbs.append((db_id, body))
        new_opts = body["properties"]["collection"]["select"]["options"]
        self.activity_db["properties"]["collection"]["select"]["options"] = new_opts
        return self.activity_db


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("NOTION_SYNC_PARENT_PAGE_ID", "parent-uuid")
    monkeypatch.setenv("NOTION_SYNC_ACTIVITY_DB_ID", "activity-uuid")
    import notion_sync.config as cfg
    cfg.invalidate()


def _coll(name, fields):
    return {"id": f"{name}-id", "name": name, "fields": fields}


def test_basic_text_collection_creates_title_plus_richtext():
    coll = _coll("ideas", [
        {"name": "title", "type": "text", "required": True},
        {"name": "summary", "type": "editor"},
        {"name": "url", "type": "url"},
    ])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    new_id = provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    assert new_id == "new-db-uuid"
    props = nc.created_dbs[0]["properties"]
    assert props["Title"] == {"title": {}}
    assert props["Summary"] == {"rich_text": {}}
    assert props["Url"] == {"url": {}}
    assert props["pb_id"] == {"rich_text": {}}
    assert props["last_synced_at"] == {"date": {}}


def test_select_field_maxselect_1_becomes_select():
    coll = _coll("ideas", [
        {"name": "title", "type": "text"},
        {"name": "status", "type": "select", "maxSelect": 1,
         "values": ["Open", "Done"]},
    ])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    props = nc.created_dbs[0]["properties"]
    assert props["Status"] == {"select": {"options": [
        {"name": "Open"}, {"name": "Done"},
    ]}}


def test_select_field_maxselect_3_becomes_multi_select():
    coll = _coll("ideas", [
        {"name": "title", "type": "text"},
        {"name": "tags", "type": "select", "maxSelect": 3,
         "values": ["a", "b", "c"]},
    ])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    props = nc.created_dbs[0]["properties"]
    assert props["Tags"]["multi_select"]["options"] == [
        {"name": "a"}, {"name": "b"}, {"name": "c"},
    ]


def test_relation_to_synced_target_becomes_relation():
    days_coll = _coll("days", [{"name": "name", "type": "text"}])
    stops_coll = _coll("stops", [
        {"name": "name", "type": "text"},
        {"name": "day", "type": "relation",
         "collectionId": "days-id", "maxSelect": 1},
    ])
    pb = FakePB({"days": days_coll, "stops": stops_coll}, [
        {"id": "1", "collection": "days", "notion_db_id": "days-notion-uuid",
         "enabled": True, "auto_sync": True, "title_field": "name"},
    ])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="stops", title_field="name",
    )
    props = nc.created_dbs[0]["properties"]
    assert props["Day"] == {
        "relation": {"database_id": "days-notion-uuid", "single_property": {}},
    }


def test_relation_to_unsynced_target_is_skipped():
    days_coll = _coll("days", [{"name": "name", "type": "text"}])
    stops_coll = _coll("stops", [
        {"name": "name", "type": "text"},
        {"name": "day", "type": "relation",
         "collectionId": "days-id", "maxSelect": 1},
    ])
    pb = FakePB({"days": days_coll, "stops": stops_coll}, [])   # no sync_config
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="stops", title_field="name",
    )
    props = nc.created_dbs[0]["properties"]
    assert "Day" not in props


def test_password_field_is_skipped():
    coll = _coll("users", [
        {"name": "name", "type": "text"},
        {"name": "password", "type": "password"},
    ])
    pb = FakePB({"users": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="users", title_field="name",
    )
    props = nc.created_dbs[0]["properties"]
    assert "Password" not in props


def test_unknown_title_field_raises():
    coll = _coll("ideas", [{"name": "title", "type": "text"}])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    with pytest.raises(RuntimeError, match="not a field"):
        provisioner.provision_notion_db(
            pb=pb, nc=nc, collection="ideas", title_field="nope",
        )


def test_missing_collection_raises():
    pb = FakePB({}, [])
    nc = FakeNotion()
    with pytest.raises(RuntimeError, match="not found"):
        provisioner.provision_notion_db(
            pb=pb, nc=nc, collection="nope", title_field="title",
        )


def test_sync_activity_option_appended():
    coll = _coll("ideas", [{"name": "title", "type": "text"}])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    assert len(nc.patched_dbs) == 1
    _, patch = nc.patched_dbs[0]
    names = [o["name"] for o in patch["properties"]["collection"]["select"]["options"]]
    assert "ideas" in names


def test_ensure_pipeline_fields_adds_pipeline_and_autodate_when_absent():
    coll = _coll("ideas", [{"name": "title", "type": "text"}])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    field_names = {f["name"] for f in pb.collections["ideas"]["fields"]}
    # Pipeline fields:
    assert "notion_id" in field_names
    assert "notion_last_edited" in field_names
    assert "last_synced_at" in field_names
    # Autodate fields (for change detection):
    assert "created" in field_names
    assert "updated" in field_names


def test_ensure_pipeline_fields_is_idempotent_when_already_present():
    coll = _coll("ideas", [
        {"name": "title", "type": "text"},
        {"name": "notion_id", "type": "text", "max": 100},
        {"name": "notion_last_edited", "type": "date"},
        {"name": "last_synced_at", "type": "date"},
        {"name": "created", "type": "autodate", "onCreate": True, "onUpdate": False},
        {"name": "updated", "type": "autodate", "onCreate": True, "onUpdate": True},
    ])
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    # Call provision (which calls ensure_pipeline_fields). Then directly
    # call ensure again — both should be no-ops on the fields side.
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    report = provisioner.ensure_pipeline_fields(pb, "ideas")
    assert report["fields_added"] == []
    pf = [f for f in pb.collections["ideas"]["fields"]
          if f["name"] in {"notion_id", "notion_last_edited", "last_synced_at",
                            "created", "updated"}]
    assert len(pf) == 5


def test_ensure_pipeline_fields_adds_only_missing_autodate_when_pipeline_present():
    coll = _coll("ideas", [
        {"name": "title", "type": "text"},
        {"name": "notion_id", "type": "text", "max": 100},
        {"name": "notion_last_edited", "type": "date"},
        {"name": "last_synced_at", "type": "date"},
        # NO created/updated
    ])
    pb = FakePB({"ideas": coll}, [])
    report = provisioner.ensure_pipeline_fields(pb, "ideas")
    assert set(report["fields_added"]) == {"created", "updated"}
    field_names = {f["name"] for f in pb.collections["ideas"]["fields"]}
    assert "created" in field_names
    assert "updated" in field_names
    # And the existing pipeline fields are still there exactly once each:
    pipeline = [f for f in pb.collections["ideas"]["fields"]
                if f["name"] in {"notion_id", "notion_last_edited", "last_synced_at"}]
    assert len(pipeline) == 3


def test_ensure_pipeline_fields_adds_unique_index_on_notion_id():
    coll = _coll("ideas", [{"name": "title", "type": "text"}])
    coll["indexes"] = []
    pb = FakePB({"ideas": coll}, [])
    nc = FakeNotion()
    provisioner.provision_notion_db(
        pb=pb, nc=nc, collection="ideas", title_field="title",
    )
    indexes = pb.collections["ideas"]["indexes"]
    assert any("idx_ideas_notion_id" in idx for idx in indexes)
    assert any("UNIQUE" in idx.upper() for idx in indexes)
    assert any("notion_id != ''" in idx for idx in indexes)
