"""Exception hierarchy for the PB client."""
from __future__ import annotations

from typing import Any


class PBError(Exception):
    """Base for all PB client errors."""


class PBNetworkError(PBError):
    """Network failure after exhausting retries.

    Attributes:
        method, path: of the failing request.
        attempts: number of attempts before giving up.
        last_error: final underlying exception (URLError, etc.).
    """
    def __init__(self, method: str, path: str, attempts: int,
                 last_error: Exception):
        super().__init__(
            f"PB {method} {path}: network failure after {attempts} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        )
        self.method = method
        self.path = path
        self.attempts = attempts
        self.last_error = last_error


class PBHTTPError(PBError):
    """Non-401 unexpected HTTP status from PB.

    Attributes:
        code: HTTP status code.
        body: parsed JSON if possible, raw text otherwise.
        method, path: of the failing request.
    """
    def __init__(self, code: int, body: Any, method: str, path: str):
        msg = (
            body.get("message") if isinstance(body, dict) else None
        ) or (
            body.get("error") if isinstance(body, dict) else None
        ) or (body if isinstance(body, str) else "")
        super().__init__(f"PB {method} {path}: HTTP {code} — {msg or body!r}")
        self.code = code
        self.body = body
        self.method = method
        self.path = path


class PBAuthError(PBError):
    """401 persisted after a forced re-auth attempt."""
    def __init__(self, method: str, path: str):
        super().__init__(
            f"PB {method} {path}: 401 persisted after forced re-auth"
        )
        self.method = method
        self.path = path
