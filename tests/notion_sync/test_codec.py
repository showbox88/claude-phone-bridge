"""Codec round-trip and edge-case tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.codec import (
    snake_to_title,
    title_to_snake,
    pb_field_to_notion_property,
    notion_property_to_pb_field,
)


def test_snake_to_title_basic():
    assert snake_to_title("departure_time") == "Departure Time"
    assert snake_to_title("name") == "Name"
    assert snake_to_title("date_start") == "Date Start"


def test_title_to_snake_basic():
    assert title_to_snake("Departure Time") == "departure_time"
    assert title_to_snake("Name") == "name"
    assert title_to_snake("Date Start") == "date_start"


def test_pb_text_to_notion_rich_text():
    out = pb_field_to_notion_property("hello", pb_type="text")
    assert out == {"rich_text": [{"type": "text", "text": {"content": "hello"}}]}


def test_pb_number_to_notion():
    assert pb_field_to_notion_property(42, pb_type="number") == {"number": 42}
    assert pb_field_to_notion_property(None, pb_type="number") == {"number": None}


def test_pb_bool_to_notion_checkbox():
    assert pb_field_to_notion_property(True, pb_type="bool") == {"checkbox": True}


def test_pb_date_to_notion():
    out = pb_field_to_notion_property("2026-06-15", pb_type="date")
    assert out == {"date": {"start": "2026-06-15"}}


def test_pb_datetime_to_notion():
    out = pb_field_to_notion_property("2026-06-15 09:00:00.000Z", pb_type="date")
    assert out["date"]["start"].startswith("2026-06-15")


def test_pb_select_single_to_notion():
    out = pb_field_to_notion_property("Done", pb_type="select", max_select=1)
    assert out == {"select": {"name": "Done"}}


def test_pb_select_multi_to_notion():
    out = pb_field_to_notion_property(["A", "B"], pb_type="select", max_select=5)
    assert out == {"multi_select": [{"name": "A"}, {"name": "B"}]}


def test_pb_empty_text_to_notion_empty():
    out = pb_field_to_notion_property("", pb_type="text")
    assert out == {"rich_text": []}


def test_notion_rich_text_to_pb():
    notion_prop = {"type": "rich_text",
                   "rich_text": [{"plain_text": "hello"}, {"plain_text": " world"}]}
    assert notion_property_to_pb_field(notion_prop, pb_type="text") == "hello world"


def test_notion_title_to_pb():
    notion_prop = {"type": "title",
                   "title": [{"plain_text": "Trip to Paris"}]}
    assert notion_property_to_pb_field(notion_prop, pb_type="text") == "Trip to Paris"


def test_notion_number_to_pb():
    assert notion_property_to_pb_field({"type": "number", "number": 42}, pb_type="number") == 42
    assert notion_property_to_pb_field({"type": "number", "number": None}, pb_type="number") is None


def test_notion_checkbox_to_pb():
    assert notion_property_to_pb_field({"type": "checkbox", "checkbox": True}, pb_type="bool") is True


def test_notion_date_to_pb():
    notion_prop = {"type": "date", "date": {"start": "2026-06-15"}}
    assert notion_property_to_pb_field(notion_prop, pb_type="date") == "2026-06-15"


def test_notion_date_none_to_pb():
    assert notion_property_to_pb_field({"type": "date", "date": None}, pb_type="date") == ""


def test_notion_select_to_pb():
    notion_prop = {"type": "select", "select": {"name": "Done"}}
    assert notion_property_to_pb_field(notion_prop, pb_type="select", max_select=1) == "Done"


def test_notion_multi_select_to_pb():
    notion_prop = {"type": "multi_select",
                   "multi_select": [{"name": "A"}, {"name": "B"}]}
    assert notion_property_to_pb_field(notion_prop, pb_type="select", max_select=5) == ["A", "B"]


def test_roundtrip_text():
    pb_val = "departure at 09:00"
    notion = pb_field_to_notion_property(pb_val, pb_type="text")
    notion_resp = {"type": "rich_text", **notion}
    back = notion_property_to_pb_field(notion_resp, pb_type="text")
    assert back == pb_val


def test_roundtrip_multi_select():
    pb_val = ["X", "Y", "Z"]
    notion = pb_field_to_notion_property(pb_val, pb_type="select", max_select=5)
    notion_resp = {"type": "multi_select", **notion}
    back = notion_property_to_pb_field(notion_resp, pb_type="select", max_select=5)
    assert back == pb_val


def test_snake_to_title_handles_consecutive_underscores():
    # Defensive — not expected in real PB fields but shouldn't double-space.
    assert snake_to_title("foo__bar") == "Foo Bar"
    assert snake_to_title("__leading") == "Leading"
    assert snake_to_title("trailing__") == "Trailing"


def test_rich_text_str_guards_non_dict_items():
    # Formula/rollup occasionally returns weird shapes; don't crash.
    notion_prop = {"type": "rich_text",
                   "rich_text": [None, {"plain_text": "hello"}, "stringy"]}
    assert notion_property_to_pb_field(notion_prop, pb_type="text") == "hello"
