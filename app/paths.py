"""Shared filesystem paths.

Single source for "where does Phone Bridge keep state/uploads/logs".
Replaces:
- `Path(__file__).parent / ".bridge_data"` scattered in server.py
- Hardcoded `/home/dev/phone-bridge/.bridge_data/sync.log` in pb_tools.py
- `os.environ.get("BRIDGE_DATA_DIR")` in notion_sync/logger.py

The repo root is the parent of the `app/` directory.
"""
from __future__ import annotations

import os
from pathlib import Path


# Project root: the parent of app/ (i.e. the directory containing server.py).
BRIDGE_ROOT: Path = Path(__file__).resolve().parent.parent

# State directory — preserved across deploys (in .deploy.json keep_files).
# Override hook for tests via BRIDGE_DATA_DIR.
DATA_DIR: Path = Path(os.environ.get("BRIDGE_DATA_DIR") or (BRIDGE_ROOT / ".bridge_data"))

# Common file targets under DATA_DIR.
SYNC_LOG: Path = DATA_DIR / "sync.log"
TODAY_ACK: Path = DATA_DIR / "today_ack.json"
SYNC_ALERT_STATE: Path = DATA_DIR / "sync_alert_state.json"
AUTH_FILE: Path = DATA_DIR / ".bridge_auth.json"

# Legacy: push_subs lives at repo root (not DATA_DIR) for historical reasons.
PUSH_SUBS: Path = BRIDGE_ROOT / "push_subs.json"

# Gmail credentials (provisioned via gmail_oauth_setup.py).
GMAIL_DIR: Path = BRIDGE_ROOT / ".gmail"

# Upload directory NAME (resolved per-session against state.cwd_root in server.py).
UPLOADS_DIRNAME: str = ".bridge_uploads"


def ensure_data_dir() -> Path:
    """Create DATA_DIR if missing. Return it. Idempotent."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
