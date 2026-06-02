"""Sync wrapper around PocketBase REST API.

Mirrors the auth pattern from `pb_tools.py` but as plain blocking functions
suitable for scripts (no async / MCP scaffolding).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class PBClient:
    def __init__(self,
                 url: str | None = None,
                 email: str | None = None,
                 password: str | None = None) -> None:
        self.url = (url or os.environ["POCKETBASE_URL"]).rstrip("/")
        self.email = email or os.environ["POCKETBASE_ADMIN_EMAIL"]
        self.password = password or os.environ["POCKETBASE_ADMIN_PASSWORD"]
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def _http(self, method: str, path: str, body: Any | None = None,
              authed: bool = True) -> Any:
        url = f"{self.url}{path}"
        headers = {"Content-Type": "application/json"}
        if authed:
            headers["Authorization"] = self._auth()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30.0) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"PB {method} {path}: {e.code} {raw[:500]}") from None

    def _auth(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        data = self._http("POST",
                          "/api/collections/_superusers/auth-with-password",
                          body={"identity": self.email, "password": self.password},
                          authed=False)
        self._token = data["token"]
        self._token_expiry = time.time() + 25 * 60
        return self._token

    def list_records(self, collection: str, *,
                     filter: str = "", sort: str = "-created",
                     per_page: int = 200) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            params = [f"page={page}", f"perPage={per_page}",
                      f"sort={urllib.parse.quote(sort, safe=',-')}"]
            if filter:
                params.append("filter=" + urllib.parse.quote(filter, safe=""))
            data = self._http("GET",
                              f"/api/collections/{collection}/records?" + "&".join(params))
            items = data.get("items", [])
            out.extend(items)
            if page >= data.get("totalPages", 1):
                break
            page += 1
        return out

    def get_record(self, collection: str, record_id: str) -> dict:
        return self._http("GET", f"/api/collections/{collection}/records/{record_id}")

    def create_record(self, collection: str, data: dict) -> dict:
        return self._http("POST", f"/api/collections/{collection}/records", body=data)

    def update_record(self, collection: str, record_id: str, data: dict) -> dict:
        return self._http("PATCH",
                          f"/api/collections/{collection}/records/{record_id}",
                          body=data)

    def delete_record(self, collection: str, record_id: str) -> None:
        self._http("DELETE", f"/api/collections/{collection}/records/{record_id}")

    def list_collections(self) -> list[dict]:
        return self._http("GET", "/api/collections?perPage=200").get("items", [])
