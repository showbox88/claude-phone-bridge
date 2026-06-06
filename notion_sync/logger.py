"""Structured operational event log for the sync runner.

One JSON line per significant operational event (run_start, run_end,
apply_error, skipped_paused, bad_timezone). Tail-able.

Conflicts and deletions are NOT written here — they go to the Sync
Activity Notion DB via notion_sync.activity helpers so the user can
review snapshots and pick a winner.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# notion_sync runs as a subprocess via `python -m notion_sync.runner`;
# add the parent dir to sys.path so `app` is importable. Phase 2 cleans
# this up by moving everything under app/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.paths import SYNC_LOG  # noqa: E402


def _log_path() -> Path:
    SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
    return SYNC_LOG


def log_event(event: str, **fields) -> None:
    """Append a JSON line. `event` is the discriminator
    (e.g. 'run_start', 'run_end', 'apply_error', 'skipped_paused')."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "event": event, **fields}
    line = json.dumps(rec, ensure_ascii=False)
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(line + "\n")
