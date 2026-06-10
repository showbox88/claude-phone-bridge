"""Post-pass phases: notify_pending + cleanup_resolved_activity.

These run AFTER the per-collection sync loop in main(). They're
independent of the per-collection logic:
  - cleanup_resolved_activity: archive Sync Activity rows older than N days
  - notify_pending: create an in-app Phone Bridge chat session listing
    Pending Sync Activity rows that need user attention

Phase 5 Task 14 split this out of runner.py. The Task 12 fix (db
imported at module level so notify_pending works) carries forward —
`db` is imported below.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db   # noqa: E402  (project root added to sys.path by runner/bootstrap)

from app.io_utils import read_json_safe, write_json_atomic
from app.paths import DATA_DIR, SYNC_ALERT_STATE
from app.settings import settings

from notion_sync.logger import log_event
from notion_sync.notion_api import NotionClient


_ALERT_STATE_FILE = "sync_alert_state.json"
_ALERT_DEDUPE_SECONDS = 6 * 3600   # 6 hours


def cleanup_resolved_activity(nc: NotionClient, *, days: int = 90) -> int:
    """Archive Sync Activity rows whose applied_at is older than `days`.

    Keeps the queue table small over time. Archives (not hard-deletes)
    so the user can un-archive in Notion to recover a row if needed.
    Returns the number archived.
    """
    db_id = settings.notion_sync_activity_db_id
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = nc.query_database(db_id, filter_={"and": [
        {"property": "applied_at", "date": {"is_not_empty": True}},
        {"property": "applied_at", "date": {"before": cutoff}},
    ]})
    archived = 0
    for r in rows:
        try:
            nc.update_page(r["id"], archived=True)
            archived += 1
        except Exception as e:
            log_event("cleanup_error", page=r.get("id"), error=str(e))
    return archived


def notify_pending(nc: NotionClient) -> int:
    """Create a Phone Bridge chat session listing Pending Sync Activity rows.

    Same UX as the weekly report: the session appears in the sidebar so
    the next time the user opens Phone Bridge they see it. Tap → talk
    to Claude inline ("帮我看看这条冲突应该选哪个").

    Dedupe: only create a new session if the last one was created more
    than 6 hours ago OR the pending row-id set has changed since the
    last alert. State lives in .bridge_data/sync_alert_state.json.

    Returns the Pending count regardless of whether a session was made.
    """
    db_id = settings.notion_sync_activity_db_id
    rows = nc.query_database(db_id, filter_={"and": [
        {"property": "decision",   "select": {"equals": "Pending"}},
        {"property": "applied_at", "date":   {"is_empty": True}},
    ]})
    n = len(rows)
    if n == 0:
        return 0

    current_ids = sorted(r["id"] for r in rows)
    if _alert_already_sent(current_ids):
        log_event("alert_skipped", reason="recent + same set", pending=n)
        return n

    title = f"📋 同步待确认 {n} 项"
    md = _render_pending_markdown(rows)

    try:
        # The bridge sqlite path matches server.py's wiring.
        db.init(DATA_DIR / "bridge.db")
        sid = db.create_session(
            cwd=settings.default_cwd or "/home/dev",
            title=title[:80], mode="chat", model="",
        )
        db.append_message(sid, "assistant_text", {"text": md})
        log_event("alert_session_created", session_id=sid, pending=n)
        _save_alert_state(current_ids)
    except Exception as e:
        log_event("alert_failed", reason=str(e), pending=n)
    return n


def _render_pending_markdown(rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"## 📋 同步待确认 {len(rows)} 项")
    lines.append("")
    lines.append("以下条目两边数据不一致(或一边消失了),需要你裁决:")
    lines.append("")
    by_op: dict[str, list[dict]] = {}
    for r in rows:
        p = r.get("properties", {})
        op = (p.get("op", {}).get("select") or {}).get("name", "?")
        by_op.setdefault(op, []).append(r)
    op_label = {
        "Conflict":           "🔀 冲突(两边都改了同一字段)",
        "Delete?":            "🗑️ 删除?(一边的记录消失了)",
        "Possible duplicate": "👯 可能重复(初次对齐发现)",
        "Schema mismatch":    "🧬 字段对不上",
    }
    for op, items in by_op.items():
        lines.append(f"### {op_label.get(op, op)} — {len(items)} 项")
        lines.append("")
        for r in items:
            p = r.get("properties", {})
            coll = (p.get("collection", {}).get("select") or {}).get("name", "?")
            summ = "".join(rt.get("plain_text", "")
                            for rt in p.get("summary", {}).get("rich_text", []))
            link = r.get("url") or ""
            lines.append(f"- **{coll}** · {summ}")
            if link:
                lines.append(f"  - [打开 Sync Activity 那一行]({link})")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**怎么处理:** 打开 Sync Activity DB(用上面任意链接),把每行的 "
                  "`Decision` 改成 `Use Notion` / `Use PB` / `Delete both` / `Keep both`。"
                  "下一次同步(每天 03:00 ET,或叫 Claude `同步一下`)会自动执行你的选择。")
    return "\n".join(lines)


def _alert_state_path() -> str:
    # Kept for back-compat with anything that imports it; new code uses
    # app.paths.SYNC_ALERT_STATE directly.
    return str(SYNC_ALERT_STATE)


def _alert_already_sent(current_ids: list[str]) -> bool:
    state = read_json_safe(SYNC_ALERT_STATE, default=None)
    if not state:
        return False
    last_ts = float(state.get("last_alert_ts") or 0)
    last_ids = state.get("last_pending_ids") or []
    now = datetime.now(timezone.utc).timestamp()
    same_set = list(current_ids) == list(last_ids)
    fresh = (now - last_ts) < _ALERT_DEDUPE_SECONDS
    return fresh and same_set


def _save_alert_state(current_ids: list[str]) -> None:
    state = {
        "last_alert_ts":     datetime.now(timezone.utc).timestamp(),
        "last_pending_ids":  list(current_ids),
    }
    try:
        write_json_atomic(SYNC_ALERT_STATE, state)
    except OSError:
        pass
