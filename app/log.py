"""Structured logging foundation (Phase 6a Task 0).

Replaces stdlib `logging.getLogger("bridge")` with structlog that
automatically injects `session_id` / `cb_id` from contextvars into
every log line. JSON-per-line output for systemd journal.

`configure()` is called once from `app/main.py` lifespan.
Modules use `get_logger("bridge")`. Callers bind contextvars:
- `bind_session(sid)` — turn boundary in agent/turn.py
- `bind_cb(id)` — permission gate boundary in agent/permission.py

request_id auto-injection is deferred to Phase 6b alongside the
CSRF/Origin work that adds the request-id middleware.
"""
from __future__ import annotations

import contextvars
import logging

import structlog


_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None)
_cb_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cb_id", default=None)


def _inject_contextvars(_, __, event_dict):
    sid = _session_id.get()
    if sid:
        event_dict["session_id"] = sid
    cb = _cb_id.get()
    if cb:
        event_dict["cb_id"] = cb
    return event_dict


def configure(level: int = logging.INFO) -> None:
    """Idempotent setup. Call once from lifespan."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_contextvars,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # Route stdlib logging through the JSON renderer too. force=True
    # overrides any prior basicConfig.
    logging.basicConfig(level=level, format="%(message)s", force=True)


def get_logger(name: str = "bridge"):
    """structlog BoundLogger keyed by `name`."""
    return structlog.get_logger(name)


def bind_session(session_id: str | None) -> None:
    """Set session_id for the current async context. Auto-injected
    into every subsequent log line in this task/turn."""
    _session_id.set(session_id)


def bind_cb(cb_id: str | None) -> None:
    """Set cb_id (permission callback id) for the current scope."""
    _cb_id.set(cb_id)
