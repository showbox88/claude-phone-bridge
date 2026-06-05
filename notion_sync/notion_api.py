"""Sync wrapper around Notion REST API.

Stdlib urllib only. Rate-limits to 2 req/s globally so we stay under
Notion's 3 req/s burst limit.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

NOTION_API_VERSION = "2022-06-28"


class NotionClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ["NOTION_TOKEN"]
        self._rate_lock = threading.Lock()
        self._last_call_at: float = 0.0

    def _throttle(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            wait = 0.5 - (now - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    def _http(self, method: str, path: str, body: Any | None = None) -> Any:
        self._throttle()
        url = f"https://api.notion.com/v1{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30.0) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"Notion {method} {path}: {e.code} {raw[:500]}") from None

    def query_database(self, database_id: str, *,
                       filter_: dict | None = None,
                       sorts: list[dict] | None = None,
                       page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        start_cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if filter_: body["filter"] = filter_
            if sorts: body["sorts"] = sorts
            if start_cursor: body["start_cursor"] = start_cursor
            data = self._http("POST", f"/databases/{database_id}/query", body=body)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")
        return out

    def retrieve_database(self, database_id: str) -> dict:
        return self._http("GET", f"/databases/{database_id}")

    def update_database(self, database_id: str, body: dict) -> dict:
        return self._http("PATCH", f"/databases/{database_id}", body=body)

    def create_database(self, parent_page_id: str, title: str,
                        properties: dict) -> dict:
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        return self._http("POST", "/databases", body=body)

    def retrieve_page(self, page_id: str) -> dict:
        return self._http("GET", f"/pages/{page_id}")

    def create_page(self, database_id: str, properties: dict,
                    icon: dict | None = None) -> dict:
        body: dict = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        if icon is not None:
            body["icon"] = icon
        return self._http("POST", "/pages", body=body)

    def update_page(self, page_id: str, properties: dict | None = None,
                    archived: bool | None = None,
                    icon: dict | None = None) -> dict:
        body: dict[str, Any] = {}
        if properties is not None: body["properties"] = properties
        if archived is not None: body["archived"] = archived
        if icon is not None: body["icon"] = icon
        return self._http("PATCH", f"/pages/{page_id}", body=body)
