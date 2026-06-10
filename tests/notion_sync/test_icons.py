"""Unit tests for the Notion icon policy."""
from notion_sync import icons


def test_day_icon_is_calendar():
    assert icons.icon_for_day() == {"type": "emoji", "emoji": "📅"}


def test_trip_icon_is_airplane():
    assert icons.icon_for_trip() == {"type": "emoji", "emoji": "✈️"}


def test_stop_icon_empty_falls_back():
    assert icons.icon_for_stop([]) == {"type": "emoji", "emoji": "📍"}
    assert icons.icon_for_stop(None) == {"type": "emoji", "emoji": "📍"}
    assert icons.icon_for_stop("") == {"type": "emoji", "emoji": "📍"}


def test_stop_icon_each_single_category():
    assert icons.icon_for_stop(["酒店"]) == {"type": "emoji", "emoji": "🏨"}
    assert icons.icon_for_stop(["餐厅"]) == {"type": "emoji", "emoji": "🍜"}
    assert icons.icon_for_stop(["购物"]) == {"type": "emoji", "emoji": "🛍️"}
    assert icons.icon_for_stop(["交通"]) == {"type": "emoji", "emoji": "🚄"}
    assert icons.icon_for_stop(["体验"]) == {"type": "emoji", "emoji": "🏛️"}
    assert icons.icon_for_stop(["打卡"]) == {"type": "emoji", "emoji": "📍"}
    assert icons.icon_for_stop(["笔记"]) == {"type": "emoji", "emoji": "📝"}
    assert icons.icon_for_stop(["消费"]) == {"type": "emoji", "emoji": "☕"}


def test_stop_icon_unknown_falls_back():
    assert icons.icon_for_stop(["nope"]) == {"type": "emoji", "emoji": "📍"}


def test_stop_icon_multi_category_priority():
    # 餐厅 (priority 2) wins over 消费 (priority 8)
    assert icons.icon_for_stop(["餐厅", "消费"]) == {"type": "emoji", "emoji": "🍜"}
    # 打卡 (priority 6) wins over 消费 (priority 8)
    assert icons.icon_for_stop(["打卡", "消费"]) == {"type": "emoji", "emoji": "📍"}
    # 酒店 (priority 1) wins over everything
    assert icons.icon_for_stop(["酒店", "餐厅", "购物", "消费"]) == {"type": "emoji", "emoji": "🏨"}
    # Input order doesn't matter — priority comes from the mapping
    assert icons.icon_for_stop(["消费", "餐厅"]) == {"type": "emoji", "emoji": "🍜"}


def test_stop_icon_accepts_string_singleton():
    assert icons.icon_for_stop("酒店") == {"type": "emoji", "emoji": "🏨"}


def test_icon_for_dispatch_days():
    assert icons.icon_for("days", {}) == {"type": "emoji", "emoji": "📅"}
    # Day icon is constant regardless of row data
    assert icons.icon_for("days", {"name": "anything"}) == {"type": "emoji", "emoji": "📅"}


def test_icon_for_dispatch_trips():
    assert icons.icon_for("trips", {}) == {"type": "emoji", "emoji": "✈️"}


def test_icon_for_dispatch_stops():
    assert icons.icon_for("stops", {"categories": ["餐厅"]}) == {"type": "emoji", "emoji": "🍜"}
    assert icons.icon_for("stops", {"categories": []}) == {"type": "emoji", "emoji": "📍"}
    assert icons.icon_for("stops", {}) == {"type": "emoji", "emoji": "📍"}


def test_icon_for_dispatch_returns_none_for_other_collections():
    # Without icon_field / icon_default, non-legacy collections get None.
    assert icons.icon_for("contacts", {}) is None
    assert icons.icon_for("plans", {}) is None
    assert icons.icon_for("journal", {}) is None
    assert icons.icon_for("locations", {}) is None


# --- Phase 5 Task 5: declarative icon resolution -----------------------

def test_icon_for_legacy_collection_ignores_declarative_kwargs():
    """The 6 legacy collections keep their domain mapping and ignore
    icon_field / icon_default entirely."""
    # days → always 📅, even if we pass icon_default="📦"
    assert icons.icon_for("days", {}, icon_default="📦") == {"type": "emoji", "emoji": "📅"}
    # trips → always ✈️
    assert icons.icon_for("trips", {"icon": "🐶"}, icon_field="icon",
                          icon_default="📦") == {"type": "emoji", "emoji": "✈️"}
    # stops → categories-based, ignores icon_field even when row has it
    assert icons.icon_for("stops", {"categories": ["餐厅"], "icon": "🐶"},
                          icon_field="icon") == {"type": "emoji", "emoji": "🍜"}
    # todos / foods / expenses likewise use their helpers, not the declarative path
    assert icons.icon_for("todos", {"status": "Done"},
                          icon_default="📦") == {"type": "emoji", "emoji": "✅"}


def test_icon_for_unknown_collection_uses_icon_default():
    """Unknown collection with icon_default returns that emoji."""
    assert icons.icon_for("custom_table", {}, icon_default="📦") == \
        {"type": "emoji", "emoji": "📦"}


def test_icon_for_unknown_collection_uses_icon_field():
    """Unknown collection reads icon_field's value from row; icon_default
    is the fallback only when the field is missing/empty."""
    # Field present → wins
    assert icons.icon_for("custom_table", {"icon": "🐶"},
                          icon_field="icon",
                          icon_default="📦") == {"type": "emoji", "emoji": "🐶"}
    # Field present but empty → falls back to default
    assert icons.icon_for("custom_table", {"icon": ""},
                          icon_field="icon",
                          icon_default="📦") == {"type": "emoji", "emoji": "📦"}
    # Field missing entirely → falls back to default
    assert icons.icon_for("custom_table", {},
                          icon_field="icon",
                          icon_default="📦") == {"type": "emoji", "emoji": "📦"}


def test_icon_for_unknown_collection_returns_none_without_config():
    """Unknown collection with neither icon_field nor icon_default → None."""
    assert icons.icon_for("custom_table", {"foo": "bar"}) is None
    # icon_field set but row doesn't have it, no default → None
    assert icons.icon_for("custom_table", {}, icon_field="icon") is None
