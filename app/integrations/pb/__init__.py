"""PocketBase HTTP client + helpers.

Public API (after Phase 1 complete):
    from app.integrations.pb import PBClient, AsyncPBClient
    from app.integrations.pb import PBError, PBHTTPError, PBAuthError, PBNetworkError
    from app.integrations.pb import refresh_token_into_env

Built incrementally; this commit only exposes exceptions. PBClient
and friends land in Tasks 2-3.
"""
from app.integrations.pb.exceptions import (
    PBAuthError,
    PBError,
    PBHTTPError,
    PBNetworkError,
)

__all__ = ["PBAuthError", "PBError", "PBHTTPError", "PBNetworkError"]
