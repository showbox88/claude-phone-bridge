"""Sync wrapper around Notion REST API.

Stdlib urllib only.

Rate-limiting: token bucket (3 capacity, refill 3/sec) — allows short
bursts up to capacity then steady 3 req/s. Phase 3 upgrade from the
previous fixed 0.5s sleep.

Retry: HTTP 429 honors Retry-After header (capped at 30s); 5xx
exponential backoff 0.1/0.2/0.4/0.8s × 4 retries; other 4xx fail fast.
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

_BUCKET_CAPACITY = 3
_BUCKET_REFILL_PER_SEC = 3.0
_MAX_RETRIES = 4
_BACKOFF_BASE_SEC = 0.1
_RETRY_AFTER_CAP_SEC = 30.0


class _TokenBucket:
    """Thread-safe token bucket."""
    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity,
                                  self.tokens + elapsed * self.refill_per_sec)
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                deficit = 1.0 - self.tokens
                wait = deficit / self.refill_per_sec
            time.sleep(wait)


class NotionClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ["NOTION_TOKEN"]
        self._bucket = _TokenBucket(_BUCKET_CAPACITY, _BUCKET_REFILL_PER_SEC)

    def _retry_after_sec(self, headers) -> float:
        raw = ""
        try:
            raw = headers.get("Retry-After") if hasattr(headers, "get") else ""
        except Exception:
            raw = ""
        if not raw:
            return 0.0
        try:
            return min(_RETRY_AFTER_CAP_SEC, max(0.0, float(raw)))
        except (TypeError, ValueError):
            return 0.0

    def _http(self, method: str, path: str, body: Any | None = None) -> Any:
        url = f"https://api.notion.com/v1{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)

        for attempt in range(_MAX_RETRIES + 1):
            self._bucket.take()
            try:
                with urllib.request.urlopen(req, timeout=30.0) as r:
                    raw = r.read().decode("utf-8")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < _MAX_RETRIES:
                    wait = self._retry_after_sec(e.headers) or \
                           (_BACKOFF_BASE_SEC * (2 ** attempt))
                    time.sleep(wait)
                    continue
                if 500 <= e.code < 600 and attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_BASE_SEC * (2 ** attempt))
                    continue
                raw = e.read().decode("utf-8", "replace")
                raise RuntimeError(
                    f"Notion {method} {path}: {e.code} {raw[:500]}") from None
        raise RuntimeError(f"Notion {method} {path}: retries exhausted")

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
