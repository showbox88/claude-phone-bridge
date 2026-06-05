"""Notion page icon policy.

Day pages get 📅, Trip pages get ✈️, Stop pages get an emoji derived from
their categories field with locked priority order. Categories repurpose
several emoji previously used as Day-level icons because semantic
richness belongs at the stop level, not the day container.
"""
from __future__ import annotations


DAY_ICON_EMOJI = "📅"
TRIP_ICON_EMOJI = "✈️"
STOP_DEFAULT_EMOJI = "📍"
EXPENSE_DEFAULT_EMOJI = "💸"

# Ordered most-specific → most-generic. First match wins when a stop has
# multiple categories. Keep in sync with CHECKIN.md's `categories` enum.
STOP_CATEGORY_PRIORITY: list[tuple[str, str]] = [
    ("酒店", "🏨"),
    ("餐厅", "🍜"),
    ("购物", "🛍️"),
    ("交通", "🚄"),
    ("体验", "🏛️"),
    ("打卡", "📍"),
    ("笔记", "📝"),
    ("消费", "☕"),
]

# Expense single-select category → emoji. Default 💸 for unmatched / empty.
EXPENSE_CATEGORY_EMOJI: dict[str, str] = {
    "餐饮":     "🍽️",
    "交通":     "🚗",
    "购物/日用": "🛒",
    "娱乐":     "🎉",
    "旅行":     "✈️",
    "订阅服务": "📺",
    "门票":     "🎫",
    "住宿":     "🏨",
    "代付":     "🤝",
    "其他":     "💸",
}


def _emoji(emoji: str) -> dict:
    return {"type": "emoji", "emoji": emoji}


def icon_for_day() -> dict:
    """Notion icon spec for a Day page. Always 📅."""
    return _emoji(DAY_ICON_EMOJI)


def icon_for_trip() -> dict:
    """Notion icon spec for a Trip page. Always ✈️."""
    return _emoji(TRIP_ICON_EMOJI)


def icon_for_stop(categories) -> dict:
    """Notion icon spec for a Stop page based on its categories.

    `categories` may be a list (PB multi-select), a single string, or
    None/empty. Highest-priority match wins. Unknown / empty → 📍.
    """
    if not categories:
        return _emoji(STOP_DEFAULT_EMOJI)
    if isinstance(categories, str):
        cats: set[str] = {categories}
    else:
        cats = {str(c) for c in categories if c}
    for name, emoji in STOP_CATEGORY_PRIORITY:
        if name in cats:
            return _emoji(emoji)
    return _emoji(STOP_DEFAULT_EMOJI)


def icon_for_expense(expense_category) -> dict:
    """Notion icon spec for an Expense page based on its expense_category.

    `expense_category` is a single-select string (or None). Unknown /
    empty → 💸.
    """
    if not expense_category:
        return _emoji(EXPENSE_DEFAULT_EMOJI)
    emoji = EXPENSE_CATEGORY_EMOJI.get(str(expense_category), EXPENSE_DEFAULT_EMOJI)
    return _emoji(emoji)


def icon_for(collection: str, row: dict) -> dict | None:
    """Dispatch by collection. Returns None for collections without policy."""
    if collection == "days":
        return icon_for_day()
    if collection == "trips":
        return icon_for_trip()
    if collection == "stops":
        return icon_for_stop(row.get("categories"))
    if collection == "expenses":
        return icon_for_expense(row.get("expense_category"))
    return None
