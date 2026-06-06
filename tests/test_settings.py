"""Tests for app.settings. Run: python tests/test_settings.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# All env vars that Settings may read. Each test unsets these to start
# from a known-empty state, then re-applies the test's overrides. We do
# NOT clear the whole os.environ — on Windows that breaks asyncio's
# Windows event loop init (it relies on SYSTEMROOT etc.).
_SETTINGS_ENV_KEYS = (
    "POCKETBASE_URL", "POCKETBASE_ADMIN_EMAIL", "POCKETBASE_ADMIN_PASSWORD", "PB_TOKEN",
    "HOST", "PORT", "DEFAULT_CWD", "ALLOWED_ORIGINS",
    "BRIDGE_AUTH_FILE", "BRIDGE_COOKIE_DAYS", "BRIDGE_NAME", "BRIDGE_DATA_DIR",
    "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_EMAIL",
    "FOURSQUARE_KEY", "AMAP_KEY",
    "NOTION_TOKEN", "NOTION_SYNC_ACTIVITY_DB_ID", "NOTION_SYNC_PARENT_PAGE_ID",
    "MCP_PB_HOST", "MCP_PB_PORT", "PUBLIC_URL",
)


def _build_settings(env: dict | None = None):
    """Construct a fresh Settings with a controlled subset of env vars.

    Unsets every Settings-tracked key, applies `env`, then constructs
    Settings(). Restores the original os.environ on return. Leaves
    system vars (PATH, SYSTEMROOT, etc.) untouched.
    """
    saved = {k: os.environ.get(k) for k in _SETTINGS_ENV_KEYS}
    for k in _SETTINGS_ENV_KEYS:
        os.environ.pop(k, None)
    if env:
        os.environ.update(env)
    try:
        # Construct directly — no module reload needed because BaseSettings
        # reads os.environ at instantiation time. Pass _env_file=None to
        # disable .env file loading so tests are reproducible regardless of
        # what's in the local repo's .env.
        from app.settings import Settings
        return Settings(_env_file=None)
    finally:
        for k in _SETTINGS_ENV_KEYS:
            os.environ.pop(k, None)
            if saved.get(k) is not None:
                os.environ[k] = saved[k]


def test_defaults():
    s = _build_settings({})
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.bridge_cookie_days == 30
    assert s.pocketbase_url == ""
    assert s.allowed_origins == "*"


def test_env_override():
    s = _build_settings({"PORT": "9999", "BRIDGE_NAME": "test-host"})
    assert s.port == 9999
    assert s.bridge_name == "test-host"


def test_int_coercion():
    s = _build_settings({"BRIDGE_COOKIE_DAYS": "7"})
    assert s.bridge_cookie_days == 7


def test_pocketbase_url_strips_trailing_slash():
    s = _build_settings({"POCKETBASE_URL": "http://127.0.0.1:8090/"})
    assert s.pocketbase_url == "http://127.0.0.1:8090"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
