"""Categorize PB and Notion rows into sync actions.

Pure function — no I/O, no globals. Given the rows on both sides and the
last_synced_at timestamp for the collection, returns a list of Action
dataclass instances. The runner does the I/O dispatch.

Timestamp comparison strategy: normalize the T separator and timezone
suffix to a uniform 'YYYY-MM-DD HH:MM:SS.SSSZ' form so lexicographic
order matches chronological order.
"""
from __future__ import annotations

from dataclasses import dataclass


def _norm_ts(s) -> str:
    if not s:
        return ""
    s = str(s).replace("T", " ")
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    return s


def _pb_id_from_notion(page: dict) -> str:
    prop = page.get("properties", {}).get("pb_id", {})
    return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))


@dataclass
class Action:
    pass


@dataclass
class NoChange(Action):
    pb_id: str
    notion_id: str


@dataclass
class PbOnlyChange(Action):
    pb_row: dict
    notion_id: str


@dataclass
class NotionOnlyChange(Action):
    notion_page: dict
    pb_id: str


@dataclass
class BothChanged(Action):
    pb_row: dict
    notion_page: dict


@dataclass
class PbNew(Action):
    pb_row: dict


@dataclass
class NotionNew(Action):
    notion_page: dict


@dataclass
class NotionVanished(Action):
    pb_row: dict


@dataclass
class PbVanished(Action):
    notion_page: dict


def categorize(pb_rows: list[dict],
               notion_rows: list[dict],
               *,
               last_synced_at: str) -> list[Action]:
    last = _norm_ts(last_synced_at)
    notion_by_id = {p["id"]: p for p in notion_rows}
    pb_by_id = {r["id"]: r for r in pb_rows}

    actions: list[Action] = []
    handled_notion_ids: set[str] = set()

    for pb_row in pb_rows:
        notion_id = pb_row.get("notion_id") or ""
        if not notion_id:
            actions.append(PbNew(pb_row=pb_row))
            continue

        notion_page = notion_by_id.get(notion_id)
        if notion_page is None:
            actions.append(NotionVanished(pb_row=pb_row))
            continue

        handled_notion_ids.add(notion_id)

        pb_updated = _norm_ts(pb_row.get("updated"))
        seen_notion_edit = _norm_ts(pb_row.get("notion_last_edited"))
        notion_edited = _norm_ts(notion_page.get("last_edited_time"))

        pb_changed = pb_updated > last
        notion_changed = (notion_edited > seen_notion_edit
                          if seen_notion_edit else notion_edited > last)

        if pb_changed and notion_changed:
            actions.append(BothChanged(pb_row=pb_row, notion_page=notion_page))
        elif pb_changed:
            actions.append(PbOnlyChange(pb_row=pb_row, notion_id=notion_id))
        elif notion_changed:
            actions.append(NotionOnlyChange(notion_page=notion_page, pb_id=pb_row["id"]))
        else:
            actions.append(NoChange(pb_id=pb_row["id"], notion_id=notion_id))

    for notion_page in notion_rows:
        if notion_page["id"] in handled_notion_ids:
            continue
        pb_id = _pb_id_from_notion(notion_page)
        if not pb_id:
            actions.append(NotionNew(notion_page=notion_page))
        elif pb_id not in pb_by_id:
            actions.append(PbVanished(notion_page=notion_page))
        else:
            pb_row = pb_by_id[pb_id]
            notion_edited = _norm_ts(notion_page.get("last_edited_time"))
            seen_notion_edit = _norm_ts(pb_row.get("notion_last_edited"))
            notion_changed = (notion_edited > seen_notion_edit
                              if seen_notion_edit else notion_edited > last)
            if notion_changed:
                actions.append(NotionOnlyChange(notion_page=notion_page, pb_id=pb_id))
            else:
                actions.append(NoChange(pb_id=pb_id, notion_id=notion_page["id"]))

    return actions
