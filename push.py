"""Web Push subscriptions: persist to JSON, send via VAPID."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from pywebpush import WebPushException, webpush

log = logging.getLogger("push")

SUBS_FILE = Path(__file__).parent / "push_subs.json"
_lock = threading.Lock()


def _vapid_claims() -> dict:
    return {"sub": f"mailto:{os.environ.get('VAPID_EMAIL', 'unknown@example.com')}"}


def init() -> None:
    if not SUBS_FILE.exists():
        SUBS_FILE.write_text("[]", encoding="utf-8")


def load_subs() -> list[dict]:
    if not SUBS_FILE.exists():
        return []
    try:
        return json.loads(SUBS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(subs: list[dict]) -> None:
    SUBS_FILE.write_text(json.dumps(subs, indent=2), encoding="utf-8")


def add_sub(sub: dict) -> None:
    with _lock:
        subs = load_subs()
        endpoint = sub.get("endpoint")
        if not endpoint:
            return
        if any(s.get("endpoint") == endpoint for s in subs):
            return
        subs.append(sub)
        _save(subs)
        log.info("push subscription added (total=%d)", len(subs))


def remove_sub(sub: dict) -> None:
    with _lock:
        endpoint = sub.get("endpoint") if isinstance(sub, dict) else sub
        subs = [s for s in load_subs() if s.get("endpoint") != endpoint]
        _save(subs)


def send_to_all(title: str, body: str, tag: str | None = None) -> None:
    """Send a push to every subscriber. Failures are logged, dead subs pruned."""
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not private_key:
        log.warning("VAPID_PRIVATE_KEY not set, skipping push")
        return

    payload = json.dumps({"title": title, "body": body, "tag": tag})
    dead: list[str] = []

    for sub in load_subs():
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims=_vapid_claims(),
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                dead.append(sub.get("endpoint", ""))
            else:
                log.warning("webpush failed (%s): %s", status, e)
        except Exception as e:
            log.warning("webpush error: %s", e)

    if dead:
        with _lock:
            subs = [s for s in load_subs() if s.get("endpoint") not in dead]
            _save(subs)
            log.info("pruned %d dead subscriptions", len(dead))
