"""Backup helper — uses a fake PB client."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.backup import backup_collections


class FakePB:
    def list_collections(self):
        return [{"name": "trips", "type": "base"},
                {"name": "todos", "type": "base"},
                {"name": "users", "type": "auth"}]

    def list_records(self, collection, **kw):
        if collection == "trips":
            return [{"id": "t1", "title": "Paris"}, {"id": "t2", "title": "Tokyo"}]
        if collection == "todos":
            return [{"id": "td1", "title": "Buy milk"}]
        return []


def test_backup_writes_json_per_base_collection(tmp_path):
    pb = FakePB()
    out_dir = backup_collections(pb, root=tmp_path)

    assert out_dir.exists()
    assert (out_dir / "trips.json").exists()
    assert (out_dir / "todos.json").exists()
    assert not (out_dir / "users.json").exists()

    trips = json.loads((out_dir / "trips.json").read_text(encoding="utf-8"))
    assert len(trips) == 2
    assert trips[0]["title"] == "Paris"


def test_backup_creates_timestamped_subdir(tmp_path):
    out_dir = backup_collections(FakePB(), root=tmp_path)
    assert out_dir.parent == tmp_path
    assert len(out_dir.name) == 15
    assert out_dir.name[8] == "-"
