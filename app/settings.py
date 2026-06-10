"""Centralized application settings.

Single typed source for every environment variable. Imported by both the
phone-bridge main process and the mcp_pb standalone process.

Reads from `os.environ` (loaded by systemd EnvironmentFile or shell-side
`set -a; . ./.env; set +a`). Also auto-loads `.env` at the repo root for
local dev convenience.

Usage:
    from app.settings import settings
    print(settings.pocketbase_url)
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import BRIDGE_ROOT


class Settings(BaseSettings):
    """All Phone Bridge env vars, typed and defaulted.

    Defaults match the historical `os.environ.get(KEY, DEFAULT)` calls.
    Changing a default == changing user-visible behavior; pin first, refactor later.
    """

    model_config = SettingsConfigDict(
        env_file=str(BRIDGE_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # PocketBase (shared)
    pocketbase_url: str = ""
    pocketbase_admin_email: str = ""
    pocketbase_admin_password: str = ""
    pb_token: str = ""

    # Bridge runtime
    host: str = "127.0.0.1"
    port: int = 8000
    default_cwd: str = ""
    allowed_origins: str = "*"
    bridge_auth_file: str = ""
    bridge_cookie_days: int = 90
    bridge_name: str = ""
    bridge_data_dir: str = ""

    # Push
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_email: str = "unknown@example.com"

    # POI
    foursquare_key: str = ""
    amap_key: str = ""

    # Notion sync
    notion_token: str = ""
    notion_sync_activity_db_id: str = ""
    notion_sync_parent_page_id: str = ""

    # mcp_pb
    mcp_pb_host: str = "127.0.0.1"
    mcp_pb_port: int = 8091
    mcp_pb_public_url: str = ""  # PUBLIC_URL

    @field_validator("pocketbase_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Tests construct Settings() directly instead."""
    return Settings()


# Module-level convenience.
settings: Settings = get_settings()
