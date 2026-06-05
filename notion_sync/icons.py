"""Notion page icon policy.

Day pages get 📅, Trip pages get ✈️, Stop pages get an emoji derived from
their categories field with locked priority order. Categories repurpose
several emoji previously used as Day-level icons because semantic
richness belongs at the stop level, not the day container.

Todo pages preserve any leading emoji the user manually put in the title
(via `strip_leading_emoji`) — that emoji becomes the page icon and the
PB title is stripped of it. When the title has no leading emoji, fall
back to a status-based default. Every todo must end up with an icon —
no empty case.
"""
from __future__ import annotations

import re


DAY_ICON_EMOJI = "📅"
TRIP_ICON_EMOJI = "✈️"
STOP_DEFAULT_EMOJI = "📍"
EXPENSE_DEFAULT_EMOJI = "💸"
TODO_DEFAULT_EMOJI = "📌"
FOOD_DEFAULT_EMOJI = "🍽️"
FOOD_TOP_RATING_EMOJI = "🤤"

# Todo status → fallback emoji when no leading emoji in title.
TODO_STATUS_EMOJI: dict[str, str] = {
    "Pending":   "📌",
    "Done":      "✅",
    "Cancelled": "❌",
}

# Match a leading emoji (possibly with variation selector / ZWJ sequence)
# at the start of a title. Covers the Supplementary Multilingual Plane
# range used by most pictographs (U+1F000–U+1FFFF) and the BMP Misc
# Symbols + Dingbats blocks (U+2600–U+27BF, U+2300–U+23FF for keycap-style).
_EMOJI_PATTERN = re.compile(
    r"^("
    r"[\U0001F000-\U0001FFFF⌀-➿⬀-⯿]"
    r"[️‍]*"
    r"(?:[\U0001F000-\U0001FFFF⌀-➿⬀-⯿][️‍]*)*"
    r")\s*"
)


def strip_leading_emoji(title: str) -> tuple[str, str]:
    """Return (clean_title, leading_emoji) — emoji is "" when none.

    Leading whitespace and separators after the emoji are removed; the
    rest of the title is returned verbatim.
    """
    if not title:
        return title, ""
    m = _EMOJI_PATTERN.match(title)
    if not m:
        return title, ""
    emoji = m.group(1)
    rest = title[m.end():].lstrip(" ·—-—　")
    return rest, emoji

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


def icon_for_todo(record: dict) -> dict:
    """Notion icon spec for a Todo page.

    1. If `record.icon` is set, use it (writer-side preferred path).
    2. Otherwise, if the title starts with a leading emoji (legacy /
       agent entry), reuse that emoji.
    3. Otherwise pick from `TODO_STATUS_EMOJI` by `status`.
    4. Default 📌 when nothing matches.
    """
    explicit = (record.get("icon") or "").strip()
    if explicit:
        return _emoji(explicit)
    title = record.get("title") or ""
    _, leading = strip_leading_emoji(title)
    if leading:
        return _emoji(leading)
    status = record.get("status") or ""
    return _emoji(TODO_STATUS_EMOJI.get(status, TODO_DEFAULT_EMOJI))


def icon_for_food(record: dict) -> dict:
    """Notion icon for a Food page.

    Default 🍽️. Boost to 🤤 for the top rating (5 hearts).
    """
    rating = (record.get("rating") or "").strip()
    if rating == "❤️❤️❤️❤️❤️":
        return _emoji(FOOD_TOP_RATING_EMOJI)
    return _emoji(FOOD_DEFAULT_EMOJI)


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
    if collection == "todos":
        return icon_for_todo(row)
    if collection == "foods":
        return icon_for_food(row)
    return None
