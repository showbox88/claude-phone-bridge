"""Gmail readonly client for the weekly report.

Reads `.gmail/token.json` (produced by gmail_oauth_setup.py). When the access
token expires, refreshes via the saved refresh_token and persists the new
expiry back to disk. Returns Primary-inbox messages (read + unread) inside
a date window, with just the metadata the summarizer needs.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("bridge.gmail")

TOKEN_PATH = Path(__file__).parent / ".gmail" / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
# Primary inbox only — excludes Promotions/Social/Updates/Forums tabs.
_QUERY_BASE = "category:primary"
_DEFAULT_MAX = 100  # hard ceiling per report so we never blow up the LLM prompt


def _service():
    """Return an authenticated Gmail API client, refreshing the token if needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _epoch_seconds(d: dt.datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


_WANTED = {"from", "to", "subject", "date"}


def _headers(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"]
            for h in (headers or []) if h["name"].lower() in _WANTED}


def fetch_window(start: dt.datetime, end: dt.datetime,
                 *, max_results: int = _DEFAULT_MAX) -> list[dict[str, Any]]:
    """Return Primary-inbox messages with date in [start, end). Read + unread.

    Capped at `max_results` (default 100) — if Gmail has more, we drop the
    oldest. Network/auth failures degrade to an empty list with a log line.
    """
    if not TOKEN_PATH.exists():
        log.warning("gmail: token missing at %s — skipping email section", TOKEN_PATH)
        return []
    s, e = _epoch_seconds(start), _epoch_seconds(end)
    q = f"{_QUERY_BASE} after:{s} before:{e}"
    try:
        svc = _service()
        # List message IDs (newest first by default).
        ids: list[str] = []
        page_token: str | None = None
        while True:
            page_size = min(100, max(1, max_results - len(ids)))
            resp = svc.users().messages().list(
                userId="me", q=q,
                maxResults=page_size,
                pageToken=page_token,
            ).execute()
            ids.extend(m["id"] for m in resp.get("messages") or [])
            page_token = resp.get("nextPageToken")
            if not page_token or len(ids) >= max_results:
                break
        if len(ids) >= max_results:
            log.info("gmail: window has more than %d msgs; truncating to newest %d",
                     max_results, max_results)

        # Pull metadata + snippet for each message.
        out: list[dict[str, Any]] = []
        for mid in ids[:max_results]:
            try:
                m = svc.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
            except Exception:
                log.warning("gmail: get(%s) failed; skipping", mid, exc_info=True)
                continue
            h = _headers(m.get("payload", {}).get("headers") or [])
            labels = m.get("labelIds") or []
            out.append({
                "id": mid,
                "from": h.get("from", ""),
                "subject": h.get("subject", "(无主题)"),
                "snippet": (m.get("snippet") or "").strip(),
                "date": h.get("date", ""),
                "labels": labels,
                "unread": "UNREAD" in labels,
            })
        log.info("gmail: fetched %d msgs in window [%s, %s)",
                 len(out), start.date(), end.date())
        return out
    except Exception:
        log.exception("gmail: fetch_window failed")
        return []
