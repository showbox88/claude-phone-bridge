"""Weekly report: posts a markdown summary to a new Chat session.

Replaces the old token-stats report with a Todo + Email digest.
Email integration is a stub — OAuth setup is deferred (user will configure
later); when no emails are available the email section is skipped silently.

Schedule + toggle live in db.settings under key "weekly_report":
    {
      "enabled": bool,
      "weekday": 1..7,   # ISO: 1=Mon .. 7=Sun
      "hour":    0..23,
      "minute":  0..59,
      "timezone": "America/New_York",
      "last_period_start_iso": "YYYY-MM-DD"  # week-start of the last sent report
    }
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

import db
import email_summarizer
import gmail_client
import todos_client

log = logging.getLogger("bridge.report")

SETTINGS_KEY = "weekly_report"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "weekday": 1,
    "hour": 9,
    "minute": 0,
    "timezone": "America/New_York",
    "last_period_start_iso": None,
}


def load() -> dict[str, Any]:
    raw = db.get_setting(SETTINGS_KEY, {}) or {}
    merged = {**DEFAULTS, **raw}
    merged["weekday"] = max(1, min(7, int(merged.get("weekday") or 1)))
    merged["hour"]    = max(0, min(23, int(merged.get("hour", 9))))
    merged["minute"]  = max(0, min(59, int(merged.get("minute", 0))))
    merged["enabled"] = bool(merged.get("enabled", True))
    tz = str(merged.get("timezone") or "America/New_York")
    try:
        ZoneInfo(tz)
    except Exception:
        tz = "America/New_York"
    merged["timezone"] = tz
    return merged


def save(patch: dict[str, Any]) -> dict[str, Any]:
    cur = load()
    cur.update(patch or {})
    db.set_setting(SETTINGS_KEY, cur)
    return load()


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/New_York")


def previous_week_window(now_local: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    """Monday→next Monday window covering the week ending strictly before now."""
    monday_this = (now_local - dt.timedelta(days=now_local.isoweekday() - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = monday_this - dt.timedelta(days=7)
    end = monday_this
    iso = start.isocalendar()
    last_day = end - dt.timedelta(days=1)
    label = (f"{iso.year}-W{iso.week:02d} "
             f"({start.month}/{start.day}–{last_day.month}/{last_day.day})")
    return start, end, label


def current_week_window(now_local: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    """Mon-this-week 00:00 → now. For 'show what I've done this week so far'."""
    start = (now_local - dt.timedelta(days=now_local.isoweekday() - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    iso = start.isocalendar()
    label = (f"{iso.year}-W{iso.week:02d} "
             f"({start.month}/{start.day}–{now_local.month}/{now_local.day} 至今)")
    return start, now_local, label


def next_fire(cfg: dict[str, Any], now_local: dt.datetime) -> dt.datetime:
    """Next configured weekday/hour/minute strictly after now_local."""
    target_weekday = int(cfg["weekday"])
    days_ahead = (target_weekday - now_local.isoweekday()) % 7
    candidate = now_local.replace(
        hour=int(cfg["hour"]), minute=int(cfg["minute"]),
        second=0, microsecond=0,
    ) + dt.timedelta(days=days_ahead)
    if candidate <= now_local:
        candidate += dt.timedelta(days=7)
    return candidate


_PRIO_BADGE = {"High": "🔴", "Normal": "🟡", "Low": "⚪"}


def _fmt_date(raw: str) -> str:
    """PocketBase dates look like '2026-06-02 00:00:00.000Z'. Strip time when
    it's exactly midnight; otherwise keep the HH:MM portion."""
    s = (raw or "").strip()
    if not s:
        return ""
    head = s[:10]
    if len(s) >= 16 and s[11:16] != "00:00":
        return f"{head} {s[11:16]}"
    return head


def _fmt_todo_line(t: dict[str, Any], *, show_due: bool = False,
                   show_completed: bool = False) -> str:
    badge = _PRIO_BADGE.get(t.get("priority") or "Normal", "🟡")
    title = (t.get("title") or "").strip() or "(无标题)"
    parts = [f"- {badge} {title}"]
    if show_due and t.get("due_date"):
        parts.append(f"_(due {_fmt_date(t['due_date'])})_")
    if show_completed and t.get("completed_at"):
        parts.append(f"_({_fmt_date(t['completed_at'])})_")
    tags = t.get("tags") or []
    if tags:
        parts.append(" ".join(f"`{tag}`" for tag in tags[:3]))
    return " ".join(parts)


def render_markdown(
    label: str,
    todo_data: dict[str, list[dict[str, Any]]],
    email_summary: str | None = None,
) -> str:
    out: list[str] = []
    out.append(f"# 📊 周报 · {label}\n")

    done = todo_data.get("done") or []
    out.append(f"## ✅ 本周完成 ({len(done)})")
    if done:
        for t in done:
            out.append(_fmt_todo_line(t, show_completed=True))
    else:
        out.append("> 本周没有完成的 todo。")
    out.append("")

    created = todo_data.get("created") or []
    out.append(f"## 📥 本周新建 ({len(created)})")
    if created:
        for t in created:
            out.append(_fmt_todo_line(t, show_due=True))
    else:
        out.append("> 本周没有新建 todo。")
    out.append("")

    overdue = todo_data.get("overdue") or []
    if overdue:
        out.append(f"## ⏰ 逾期未完成 ({len(overdue)})")
        for t in overdue:
            out.append(_fmt_todo_line(t, show_due=True))
        out.append("")

    upcoming = todo_data.get("upcoming") or []
    if upcoming:
        out.append(f"## 📅 下周到期 ({len(upcoming)})")
        for t in upcoming:
            out.append(_fmt_todo_line(t, show_due=True))
        out.append("")

    out.append("## 📧 本周重要邮件")
    if email_summary:
        out.append(email_summary.strip())
    else:
        out.append("> _邮件集成未配置 — 待 Gmail OAuth 完成后启用。_")
    out.append("")

    return "\n".join(out)


def _post_to_new_session(label: str, markdown: str, cwd: str) -> str:
    title = f"📊 周报 {label}"
    sid = db.create_session(cwd=cwd, title=title[:80], mode="chat", model="")
    db.append_message(sid, "assistant_text", {"text": markdown})
    return sid


def generate_for_window(start_local: dt.datetime, end_local: dt.datetime,
                        label: str, cwd: str) -> tuple[str, str]:
    try:
        todo_data = todos_client.weekly_snapshot(start_local, end_local)
    except Exception:
        log.exception("todos snapshot failed")
        todo_data = {"done": [], "created": [], "overdue": [], "upcoming": []}

    email_summary: str | None = None
    try:
        emails = gmail_client.fetch_window(start_local, end_local)
        if emails:
            email_summary = email_summarizer.summarize(emails)
        else:
            email_summary = "> 本周 Primary inbox 没有邮件 (或 Gmail 集成未就绪)。"
    except Exception:
        log.exception("email section failed")
        email_summary = None  # render_markdown emits the not-configured placeholder

    md = render_markdown(label, todo_data, email_summary)
    sid = _post_to_new_session(label, md, cwd)
    return sid, md


async def scheduler_loop(default_cwd: str, on_post=None) -> None:
    """Re-read settings each loop so toggle/time changes take effect live."""
    log.info("weekly-report scheduler started")
    while True:
        try:
            cfg = load()
            tz = _tz(cfg["timezone"])
            now_local = dt.datetime.now(tz)
            target = next_fire(cfg, now_local)
            sleep_s = max(60.0, (target - now_local).total_seconds())
            sleep_s = min(sleep_s, 3600.0)
            await asyncio.sleep(sleep_s)

            cfg = load()
            if not cfg["enabled"]:
                continue
            tz = _tz(cfg["timezone"])
            now_local = dt.datetime.now(tz)
            target = next_fire(cfg, now_local)
            if (target - now_local).total_seconds() > 60:
                continue
            start, end, label = previous_week_window(now_local)
            iso = start.date().isoformat()
            if cfg.get("last_period_start_iso") == iso:
                continue
            sid, _md = await asyncio.to_thread(
                generate_for_window, start, end, label, default_cwd
            )
            save({"last_period_start_iso": iso})
            log.info("weekly report posted: session=%s label=%s", sid, label)
            if on_post:
                try:
                    await on_post(sid, label)
                except Exception:
                    log.exception("weekly report on_post hook failed")
        except asyncio.CancelledError:
            log.info("weekly-report scheduler cancelled")
            raise
        except Exception:
            log.exception("weekly-report scheduler iteration error")
            await asyncio.sleep(300)


async def run_now(default_cwd: str, window: str = "current") -> tuple[str, str]:
    """Manually generate a report. window='current' (Mon→now) for an instant
    peek at this week so far; window='previous' for the same window the
    scheduled job would post on Monday."""
    cfg = load()
    tz = _tz(cfg["timezone"])
    now_local = dt.datetime.now(tz)
    if window == "previous":
        start, end, label = previous_week_window(now_local)
        save({"last_period_start_iso": start.date().isoformat()})
    else:
        start, end, label = current_week_window(now_local)
    sid, _md = await asyncio.to_thread(generate_for_window, start, end, label, default_cwd)
    return sid, label
