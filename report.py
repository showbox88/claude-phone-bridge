"""Weekly stats report: posts a markdown summary to a new Chat session.

Schedule + toggle live in db.settings under key "weekly_report":
    {
      "enabled": bool,
      "weekday": 1..7,   # ISO: 1=Mon .. 7=Sun
      "hour":    0..23,
      "minute":  0..59,
      "timezone": "Asia/Shanghai",
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

log = logging.getLogger("bridge.report")

SETTINGS_KEY = "weekly_report"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "weekday": 1,
    "hour": 9,
    "minute": 0,
    "timezone": "Asia/Shanghai",
    "last_period_start_iso": None,
}


def load() -> dict[str, Any]:
    raw = db.get_setting(SETTINGS_KEY, {}) or {}
    merged = {**DEFAULTS, **raw}
    merged["weekday"] = max(1, min(7, int(merged.get("weekday") or 1)))
    merged["hour"]    = max(0, min(23, int(merged.get("hour", 9))))
    merged["minute"]  = max(0, min(59, int(merged.get("minute", 0))))
    merged["enabled"] = bool(merged.get("enabled", True))
    tz = str(merged.get("timezone") or "Asia/Shanghai")
    try:
        ZoneInfo(tz)
    except Exception:
        tz = "Asia/Shanghai"
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
        return ZoneInfo("Asia/Shanghai")


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


def _fmt_money(v: float) -> str:
    v = float(v or 0)
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.4f}"


def _fmt_tokens(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_dur(ms: int) -> str:
    s = int((ms or 0) / 1000)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60}s"
    return f"{s // 3600}h{(s % 3600) // 60}m"


def render_markdown(summary: dict[str, Any], label: str) -> str:
    t = summary["totals"]
    out: list[str] = []
    out.append(f"# 📊 周报 · {label}\n")
    out.append(
        f"- 总对话轮次：**{t['turns']}** 轮\n"
        f"- 新建会话：**{summary['new_sessions']}** 个\n"
        f"- 总花销：**{_fmt_money(t['cost'])}**\n"
        f"- 总用时：**{_fmt_dur(t['duration_ms'])}**\n"
    )
    out.append("## Token")
    out.append(
        f"| 输入 | 输出 | 缓存读 | 缓存写 |\n"
        f"|---|---|---|---|\n"
        f"| {_fmt_tokens(t['in_tok'])} "
        f"| {_fmt_tokens(t['out_tok'])} "
        f"| {_fmt_tokens(t['cache_read'])} "
        f"| {_fmt_tokens(t['cache_create'])} |\n"
    )
    if summary["by_model"]:
        out.append("## 按模型")
        out.append("| 模型 | 轮次 | 花销 |")
        out.append("|---|---|---|")
        for m in summary["by_model"]:
            out.append(f"| {m['model'] or '默认'} | {m['turns']} | {_fmt_money(m['cost'])} |")
        out.append("")
    if summary["top_cwds"]:
        out.append("## 最活跃目录 Top 5")
        for c in summary["top_cwds"]:
            out.append(f"- `{c['cwd']}` — {c['turns']} 轮 · {_fmt_money(c['cost'])}")
        out.append("")
    if summary["top_sessions"]:
        out.append("## 最活跃会话 Top 5")
        for s in summary["top_sessions"]:
            title = (s["title"] or "(未命名)").strip() or "(未命名)"
            out.append(f"- {title} — {s['turns']} 轮 · {_fmt_money(s['cost'])}")
        out.append("")
    if t["turns"] == 0:
        out.append("> 本周没有任何对话记录。")
    return "\n".join(out)


def _post_to_new_session(label: str, markdown: str, cwd: str) -> str:
    title = f"📊 周报 {label}"
    sid = db.create_session(cwd=cwd, title=title[:80], mode="chat", model="")
    db.append_message(sid, "assistant_text", {"text": markdown})
    return sid


def generate_for_window(start_local: dt.datetime, end_local: dt.datetime,
                        label: str, cwd: str) -> tuple[str, str]:
    summary = db.range_summary(start_local.timestamp(), end_local.timestamp())
    md = render_markdown(summary, label)
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
