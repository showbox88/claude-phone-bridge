"""Web Push subscriptions: persist to JSON, send via VAPID."""
from __future__ import annotations

import json
import logging
import threading

from pywebpush import WebPushException, webpush

from app.io_utils import read_json_safe, write_json_atomic
from app.paths import PUSH_SUBS
from app.settings import settings

log = logging.getLogger("push")

SUBS_FILE = PUSH_SUBS
_lock = threading.Lock()


def _vapid_claims() -> dict:
    return {"sub": f"mailto:{settings.vapid_email}"}


def init() -> None:
    if not SUBS_FILE.exists():
        write_json_atomic(SUBS_FILE, [])


def load_subs() -> list[dict]:
    return read_json_safe(SUBS_FILE, default=[])


def _save(subs: list[dict]) -> None:
    write_json_atomic(SUBS_FILE, subs)


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
    private_key = settings.vapid_private_key.strip()
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
