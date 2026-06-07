"""Back-compat shim — the real PBClient now lives in app.integrations.pb.

This module exists so the 19 existing call sites (in notion_sync/,
server.py REST handlers, and scripts/) can keep `from notion_sync.pb_api
import PBClient` working unchanged. Phase 2 (server.py decomposition)
removes the shim.

Notable behavior:
  - list_records preserved (auto-paginates -> list[dict]); maps to
    app.integrations.pb.PBClient.list_all.
  - update_record uses PATCH (unchanged).
  - No-arg constructor reads settings (was already the case).
  - _http() is preserved as a back-compat alias to request() so the 3
    pb._http(...) calls in notion_sync/provisioner.py keep working
    until Task 6 swaps them to public collection methods.

If you're writing NEW code: import from app.integrations.pb instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.pb import PBClient as _UnifiedPBClient  # noqa: E402
from app.settings import Settings  # noqa: E402


class PBClient(_UnifiedPBClient):
    """Back-compat: zero-arg constructor reads from Settings()."""

    def __init__(self) -> None:
        # Settings() per call so monkeypatch.setenv in tests still works.
        s = Settings()
        super().__init__(
            s.pocketbase_url,
            s.pocketbase_admin_email,
            s.pocketbase_admin_password,
        )

    def list_records(
        self, collection: str, *, filter: str = "",
        sort: str = "-created", per_page: int = 200,
    ) -> list[dict]:
        """Auto-paginating list, preserving the legacy signature."""
        return self.list_all(
            collection, filter=filter, sort=sort, per_page=per_page,
        )

    def _http(self, method: str, path: str, *, body: dict | None = None) -> dict:
        """Back-compat alias for legacy provisioner.py callers.

        Deletes in Task 6 once provisioner.py switches to public collection
        methods (get_collection / update_collection / etc.).
        """
        return self.request(method, path, body=body)
