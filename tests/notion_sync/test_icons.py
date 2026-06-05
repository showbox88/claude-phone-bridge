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
    assert icons.icon_for("todos", {}) is None
    assert icons.icon_for("contacts", {}) is None
    assert icons.icon_for("plans", {}) is None
    assert icons.icon_for("journal", {}) is None
    assert icons.icon_for("locations", {}) is None
