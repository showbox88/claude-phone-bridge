"""PocketBase todos client for the weekly report.

Fetches four buckets in one window:
  - done:     status=Done AND completed_at in [start, end)
  - created:  created in [start, end)
  - overdue:  status=Pending AND due_date < end (open at report time)
  - upcoming: status=Pending AND due_date in [end, end+7d)

Auth uses the same _superusers credentials as server.py's _pb_refresh_token,
re-read each call so password rotation doesn't strand the report.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("bridge.todos")

_TIMEOUT = 10
_PAGE_SIZE = 200


def _pb_config() -> tuple[str, str, str]:
    url = os.environ.get("POCKETBASE_URL", "").rstrip("/")
    email = os.environ.get("POCKETBASE_ADMIN_EMAIL", "")
    password = os.environ.get("POCKETBASE_ADMIN_PASSWORD", "")
    return url, email, password


def _auth_token() -> str | None:
    url, email, password = _pb_config()
    if not (url and email and password):
        return None
    req = urllib.request.Request(
        url + "/api/collections/_superusers/auth-with-password",
        data=json.dumps({"identity": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read()).get("token")
    except (urllib.error.URLError, ValueError, OSError) as e:
        log.warning("todos: PB auth failed: %s", e)
        return None


def _list(filter_expr: str, sort: str = "-created") -> list[dict[str, Any]]:
    url, _, _ = _pb_config()
    token = _auth_token()
    if not (url and token):
        return []
    qs = urllib.parse.urlencode({
        "filter": filter_expr,
        "sort": sort,
        "perPage": _PAGE_SIZE,
        "skipTotal": "true",
    })
    req = urllib.request.Request(
        f"{url}/api/collections/todos/records?{qs}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read()).get("items") or []
    except (urllib.error.URLError, ValueError, OSError) as e:
        log.warning("todos: list failed (%s): %s", filter_expr, e)
        return []


def _pb_dt(d: dt.datetime) -> str:
    """PocketBase filter date format. Convert to UTC, naive, no microseconds."""
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def weekly_snapshot(start_local: dt.datetime,
                    end_local: dt.datetime) -> dict[str, list[dict[str, Any]]]:
    """Return {done, created, overdue, upcoming} for the [start, end) window.

    overdue = open todos whose due_date is strictly before `end_local`
              (so the user sees what's slipping at report time).
    upcoming = open todos due in the 7 days after `end_local`.
    """
    s, e = _pb_dt(start_local), _pb_dt(end_local)
    e_plus_7 = _pb_dt(end_local + dt.timedelta(days=7))

    done = _list(
        f'status = "Done" && completed_at >= "{s}" && completed_at < "{e}"',
        sort="-completed_at",
    )
    created = _list(
        f'created >= "{s}" && created < "{e}"',
        sort="-created",
    )
    overdue = _list(
        f'status = "Pending" && due_date != "" && due_date < "{e}"',
        sort="due_date",
    )
    upcoming = _list(
        f'status = "Pending" && due_date >= "{e}" && due_date < "{e_plus_7}"',
        sort="due_date",
    )
    return {"done": done, "created": created,
            "overdue": overdue, "upcoming": upcoming}
