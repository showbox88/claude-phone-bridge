"""Password + TOTP authentication with persistent device-bound sessions.

State is stored in a single JSON file (default `.bridge_auth.json`):

    {
      "password_hash": "$2b$12$...",      # bcrypt
      "totp_secret":   "BASE32...",         # pyotp
      "devices": {
        "<sha256-of-token>": {
          "name": "Office PC",
          "added_at": 1778280000,
          "last_seen": 1778280123,
          "last_ip": "100.x.x.x",
          "last_ua": "Mozilla/..."
        }
      }
    }

The plaintext device token is set as an HttpOnly cookie. Server stores only
SHA-256(token), so a leaked auth file does not expose active sessions.

Rate limiting is in-memory: 5 failures within 5 minutes from an IP triggers
a temporary lockout (cleared on first window expiry).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from threading import Lock
from typing import Optional

import bcrypt
import pyotp
import segno

COOKIE_NAME = "bridge_session"
DEFAULT_COOKIE_SECONDS = 30 * 24 * 60 * 60   # 30 days

FAIL_WINDOW_SEC = 300       # 5 minutes
FAIL_LIMIT = 5

LAST_SEEN_DEBOUNCE_SEC = 60  # only persist last_seen at most once a minute per device


def _now() -> int:
    return int(time.time())


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthState:
    """Thread-safe persistent auth store."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = Lock()
        self.data = self._load()
        # In-memory: ip -> [unix_ts of failures]
        self.failures: dict[str, list[float]] = {}
        # In-memory: token_hash -> last persisted last_seen
        self._last_seen_persisted: dict[str, int] = {}

    # ---- persistence -----------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {"password_hash": None, "totp_secret": None, "devices": {}, "super_link_hash": None}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"password_hash": None, "totp_secret": None, "devices": {}, "super_link_hash": None}

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ---- initialization -------------------------------------------------
    def is_initialized(self) -> bool:
        return bool(self.data.get("password_hash") and self.data.get("totp_secret"))

    def initialize(self, password: str) -> str:
        """Create initial password hash and TOTP secret. Returns the secret."""
        if not password or len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self.lock:
            self.data["password_hash"] = bcrypt.hashpw(
                password.encode("utf-8"), bcrypt.gensalt(rounds=12)
            ).decode("ascii")
            self.data["totp_secret"] = pyotp.random_base32()
            self.data["devices"] = {}
            self._save_locked()
            return self.data["totp_secret"]

    def totp_secret(self) -> Optional[str]:
        return self.data.get("totp_secret")

    # ---- credential verification ----------------------------------------
    def verify_password(self, password: str) -> bool:
        h = self.data.get("password_hash")
        if not h or not password:
            return False
        try:
            return bcrypt.checkpw(password.encode("utf-8"), h.encode("ascii"))
        except (ValueError, TypeError):
            return False

    def verify_totp(self, code: str) -> bool:
        secret = self.data.get("totp_secret")
        if not secret or not code:
            return False
        try:
            return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
        except Exception:
            return False

    # ---- device sessions -------------------------------------------------
    def issue_device_token(self, name: str, ip: str = "", ua: str = "") -> str:
        """Mint a fresh device token (returned plaintext)."""
        token = secrets.token_urlsafe(32)
        h = _hash_token(token)
        with self.lock:
            self.data["devices"][h] = {
                "name": name or "unnamed device",
                "added_at": _now(),
                "last_seen": _now(),
                "last_ip": ip,
                "last_ua": (ua or "")[:200],
            }
            self._save_locked()
            self._last_seen_persisted[h] = _now()
        return token

    def lookup_token(self, token: str, ip: str = "", ua: str = "") -> Optional[dict]:
        """Return device record if token is valid, else None.

        Side effect: debounced refresh of last_seen / last_ip / last_ua.
        """
        if not token:
            return None
        h = _hash_token(token)
        d = self.data.get("devices", {}).get(h)
        if not d:
            return None
        now = _now()
        last_persisted = self._last_seen_persisted.get(h, 0)
        if now - last_persisted >= LAST_SEEN_DEBOUNCE_SEC:
            with self.lock:
                d["last_seen"] = now
                if ip:
                    d["last_ip"] = ip
                if ua:
                    d["last_ua"] = ua[:200]
                self._save_locked()
                self._last_seen_persisted[h] = now
        return {**d, "hash": h}

    def revoke(self, token_hash: str) -> bool:
        with self.lock:
            removed = self.data.get("devices", {}).pop(token_hash, None)
            if removed is not None:
                self._save_locked()
            return removed is not None

    def list_devices(self) -> list[dict]:
        with self.lock:
            return [{"hash": k, **v} for k, v in self.data.get("devices", {}).items()]

    # ---- super link (hidden auth gate) ----------------------------------
    def has_super_link(self) -> bool:
        return bool(self.data.get("super_link_hash"))

    def set_super_link(self) -> str:
        """Mint a fresh super-link secret, store only its hash, return plaintext.

        Rotating (calling again) invalidates the previous link immediately.
        """
        secret = secrets.token_urlsafe(36)  # ~48 url-safe chars
        with self.lock:
            self.data["super_link_hash"] = _hash_token(secret)
            self._save_locked()
        return secret

    def verify_super_link(self, candidate: str) -> bool:
        stored = self.data.get("super_link_hash")
        if not stored or not candidate:
            return False
        return hmac.compare_digest(stored, _hash_token(candidate))

    # ---- rate limit ------------------------------------------------------
    def can_attempt(self, ip: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds_if_blocked)."""
        if not ip:
            return True, 0
        now = time.time()
        with self.lock:
            arr = [t for t in self.failures.get(ip, []) if now - t < FAIL_WINDOW_SEC]
            self.failures[ip] = arr
            if len(arr) >= FAIL_LIMIT:
                oldest = min(arr)
                wait = int(FAIL_WINDOW_SEC - (now - oldest)) + 1
                return False, max(wait, 1)
            return True, 0

    def record_fail(self, ip: str) -> None:
        if not ip:
            return
        with self.lock:
            self.failures.setdefault(ip, []).append(time.time())

    def clear_fails(self, ip: str) -> None:
        if not ip:
            return
        with self.lock:
            self.failures.pop(ip, None)


# ----------------------------------------------------------------------------
# QR / otpauth helpers
# ----------------------------------------------------------------------------

def otpauth_uri(secret: str, label: str, issuer: str = "Phone Bridge") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=label, issuer_name=issuer)


def qr_svg(data: str, scale: int = 6) -> str:
    """Return inline SVG markup for a QR encoding `data`.

    QR conventions require dark-on-light; inverted (light-on-dark) QRs are
    rejected by Google Authenticator and many other scanners. We render
    black on white and let the page CSS frame it on a white tile.
    """
    import io as _io
    qr = segno.make(data, error="m")
    out = _io.BytesIO()
    qr.save(out, kind="svg", scale=scale, dark="#000000", light="#ffffff",
            border=4, xmldecl=False)
    return out.getvalue().decode("utf-8")


# ----------------------------------------------------------------------------
# Cookie helpers (used by FastAPI route handlers)
# ----------------------------------------------------------------------------

def set_session_cookie(response, token: str, max_age: int = DEFAULT_COOKIE_SECONDS) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=True,         # served behind Tailscale Serve (HTTPS) and Funnel (HTTPS)
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def client_ip(request) -> str:
    """Best-effort client IP. Tailscale Serve forwards loopback HTTP and may
    set X-Forwarded-For; trust it since Tailscale is the only source."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host or ""
    return ""
