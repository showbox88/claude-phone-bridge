"""Side-channel helper that mirrors PB's token into os.environ.

Used by `server.py` because child Bash subprocesses spawned by Claude
SDK inherit env vars; the CHECKIN flow uses `$PB_TOKEN` and `$PB_URL`
directly in curl commands.

The 12h refresh loop in server.py calls this on schedule; the 401
fallback inside PBClient also handles token expiry, but the env
mirror only happens when refresh_token_into_env is explicitly called.
"""
from __future__ import annotations

import logging
import os

from app.integrations.pb.client import PBClient

log = logging.getLogger("app.pb.token")


def refresh_token_into_env(pb: PBClient) -> None:
    """Force-authenticate `pb` and mirror token/url into os.environ.

    Subsequent child processes inherit:
        PB_TOKEN  — current PB admin token
        PB_URL    — the PB base URL `pb` is configured for

    Raises PBAuthError on credential failure.
    """
    token = pb.authenticate()
    os.environ["PB_TOKEN"] = token
    os.environ["PB_URL"] = pb.url
    log.info("PB token refreshed (len=%d)", len(token))
