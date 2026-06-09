"""Structured operational event log for the sync runner.

One JSON line per significant operational event (run_start, run_end,
apply_error, skipped_paused, bad_timezone). Tail-able.

Conflicts and deletions are NOT written here — they go to the Sync
Activity Notion DB via notion_sync.activity helpers so the user can
review snapshots and pick a winner.

Backed by logging.handlers.RotatingFileHandler so sync.log can never
grow unbounded: 10 MB per file × 5 backups = ~50 MB max footprint.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# notion_sync runs as a subprocess via `python -m notion_sync.runner`;
# add the parent dir to sys.path so `app` is importable. Phase 2 cleans
# this up by moving everything under app/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.paths import SYNC_LOG  # noqa: E402


def _log_path() -> Path:
    SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
    return SYNC_LOG


_logger = logging.getLogger("notion_sync")
_logger.setLevel(logging.INFO)

# Idempotent handler attach — avoid double-handlers when the module is
# re-imported (e.g. tests importing notion_sync.logger multiple times).
if not any(isinstance(h, RotatingFileHandler) for h in _logger.handlers):
    _handler = RotatingFileHandler(
        str(_log_path()),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,               # sync.log + sync.log.1 .. .5
        encoding="utf-8",
    )
    # Raw JSON line, no prefix — callers grep sync.log expecting pure JSON.
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False  # don't double-log to root


def log_event(event: str, **fields) -> None:
    """Append a JSON line. `event` is the discriminator
    (e.g. 'run_start', 'run_end', 'apply_error', 'skipped_paused')."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "event": event, **fields}
    _logger.info(json.dumps(rec, ensure_ascii=False))
