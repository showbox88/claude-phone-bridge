"""PocketBase HTTP client.

Sync core (`PBClient`); async wrapper (`AsyncPBClient`) uses to_thread.

Design:
- Per-instance token cache (no module globals).
- urllib.request only — no new HTTP deps.
- request() is the single HTTP entry point; CRUD helpers call it.
- 401 → forced authenticate() + one re-request, not counted as retry.
- 5xx / 429 / network → exponential backoff with jitter, capped retries.
- 4xx ≠ 401/429 → PBHTTPError, no retry.

Tests inject a mock urlopen via `pb._urlopen = mock_callable`.
"""
from __future__ import annotations

import asyncio
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from app.integrations.pb.exceptions import (
    PBAuthError,
    PBHTTPError,
    PBNetworkError,
)
from app.log import get_logger

log = get_logger("app.pb.client")

_AUTH_PATH = "/api/collections/_superusers/auth-with-password"
_RETRY_AFTER_CAP_SECS = 30.0


class PBClient:
    def __init__(
        self,
        url: str,
        email: str,
        password: str,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        retry_initial_backoff: float = 1.0,
        retry_jitter_max: float = 0.5,
    ) -> None:
        self._url = url.rstrip("/")
        self._email = email
        self._password = password
        self._timeout = timeout
        self._retries = retries
        self._retry_initial_backoff = retry_initial_backoff
        self._retry_jitter_max = retry_jitter_max
        self._token: str | None = None
        self._urlopen: Callable = urllib.request.urlopen

    @property
    def url(self) -> str:
        return self._url

    @property
    def token(self) -> str | None:
        return self._token

    def authenticate(self) -> str:
        body = {"identity": self._email, "password": self._password}
        result = self._request_raw(
            "POST", _AUTH_PATH, body=body,
            with_auth=False, retry_on_401=False,
        )
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise PBAuthError("POST", _AUTH_PATH)
        self._token = token
        return token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        query: dict | None = None,
        timeout: float | None = None,
        retry_on_401: bool = True,
    ) -> dict:
        if not self._token:
            self.authenticate()
        return self._request_raw(
            method, path, body=body, query=query,
            timeout=timeout, with_auth=True,
            retry_on_401=retry_on_401,
        )

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        query: dict | None = None,
        timeout: float | None = None,
        with_auth: bool,
        retry_on_401: bool,
    ) -> dict:
        url = self._url + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)

        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if with_auth and self._token:
            headers["Authorization"] = self._token

        data = json.dumps(body).encode() if body is not None else None
        effective_timeout = timeout if timeout is not None else self._timeout

        last_net_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            req = urllib.request.Request(
                url, data=data, method=method, headers=headers,
            )
            try:
                resp = self._urlopen(req, timeout=effective_timeout)
                with resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return {"_raw": raw.decode("utf-8", errors="replace")}
            except urllib.error.HTTPError as e:
                err_raw = b""
                try:
                    err_raw = e.read()
                    err_body = json.loads(err_raw) if err_raw else {}
                except (json.JSONDecodeError, ValueError):
                    err_body = err_raw.decode("utf-8", errors="replace") if err_raw else ""
                except Exception:
                    err_body = ""

                if e.code == 401:
                    if not retry_on_401:
                        raise PBAuthError(method, path) from e
                    log.warning("PB %s %s: 401 — forcing re-auth", method, path)
                    self.authenticate()
                    if with_auth:
                        headers["Authorization"] = self._token or ""
                    return self._request_raw(
                        method, path, body=body, query=query,
                        timeout=timeout, with_auth=with_auth,
                        retry_on_401=False,
                    )

                if e.code == 429 or 500 <= e.code < 600:
                    wait = self._backoff_secs(attempt)
                    if e.code == 429:
                        retry_after = e.headers.get("Retry-After") if e.headers else None
                        try:
                            ra = float(retry_after) if retry_after else 0
                        except ValueError:
                            ra = 0
                        wait = min(max(wait, ra), _RETRY_AFTER_CAP_SECS)
                    log.warning(
                        "PB %s %s: HTTP %s, retry %d/%d after %.2fs",
                        method, path, e.code, attempt, self._retries, wait,
                    )
                    if attempt >= self._retries:
                        log.error(
                            "PB %s %s: HTTP %s after %d attempts",
                            method, path, e.code, attempt,
                        )
                        raise PBHTTPError(e.code, err_body, method, path) from e
                    time.sleep(wait)
                    continue

                raise PBHTTPError(e.code, err_body, method, path) from e

            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                last_net_error = e
                if attempt >= self._retries:
                    log.error(
                        "PB %s %s: %s after %d attempts",
                        method, path, type(e).__name__, attempt,
                    )
                    raise PBNetworkError(method, path, attempt, e) from e
                wait = self._backoff_secs(attempt)
                log.warning(
                    "PB %s %s: %s, retry %d/%d after %.2fs",
                    method, path, type(e).__name__, attempt, self._retries, wait,
                )
                time.sleep(wait)
                continue

        raise PBNetworkError(method, path, self._retries,
                             last_net_error or Exception("unknown"))

    def _backoff_secs(self, attempt: int) -> float:
        base = self._retry_initial_backoff * (2 ** (attempt - 1))
        jitter = random.uniform(0, self._retry_jitter_max)
        return base + jitter

    # --- Records --------------------------------------------------------

    def list_page(
        self, collection: str, *, filter: str = "",
        sort: str = "-created", expand: str = "",
        page: int = 1, per_page: int = 30,
        skip_total: bool = False,
    ) -> dict:
        query: dict = {"page": page, "perPage": per_page}
        if filter:
            query["filter"] = filter
        if sort:
            query["sort"] = sort
        if expand:
            query["expand"] = expand
        if skip_total:
            query["skipTotal"] = "true"
        return self.request(
            "GET", f"/api/collections/{collection}/records", query=query,
        )

    def list_all(
        self, collection: str, *, filter: str = "",
        sort: str = "-created", expand: str = "",
        per_page: int = 200,
    ) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            envelope = self.list_page(
                collection, filter=filter, sort=sort, expand=expand,
                page=page, per_page=per_page,
            )
            items.extend(envelope.get("items", []))
            total_pages = envelope.get("totalPages", 1)
            if page >= total_pages:
                return items
            page += 1

    def get_record(self, collection: str, record_id: str, *,
                   expand: str = "") -> dict:
        query = {"expand": expand} if expand else None
        return self.request(
            "GET",
            f"/api/collections/{collection}/records/{record_id}",
            query=query,
        )

    def create_record(self, collection: str, body: dict) -> dict:
        return self.request(
            "POST", f"/api/collections/{collection}/records", body=body,
        )

    def update_record(self, collection: str, record_id: str,
                      body: dict) -> dict:
        return self.request(
            "PATCH",
            f"/api/collections/{collection}/records/{record_id}",
            body=body,
        )

    def delete_record(self, collection: str, record_id: str) -> None:
        self.request(
            "DELETE",
            f"/api/collections/{collection}/records/{record_id}",
        )

    # --- Collections ----------------------------------------------------

    def list_collections(self) -> list[dict]:
        envelope = self.request(
            "GET", "/api/collections",
            query={"page": 1, "perPage": 200, "skipTotal": "true"},
        )
        return envelope.get("items", [])

    def get_collection(self, name_or_id: str) -> dict:
        return self.request(
            "GET", f"/api/collections/{name_or_id}",
        )

    def create_collection(self, body: dict) -> dict:
        return self.request("POST", "/api/collections", body=body)

    def update_collection(self, name_or_id: str, body: dict) -> dict:
        return self.request(
            "PATCH", f"/api/collections/{name_or_id}", body=body,
        )

    def delete_collection(self, name_or_id: str) -> None:
        self.request("DELETE", f"/api/collections/{name_or_id}")


class AsyncPBClient:
    """Wraps PBClient with asyncio.to_thread."""

    def __init__(self, *args, **kwargs) -> None:
        self._sync = PBClient(*args, **kwargs)

    @property
    def url(self) -> str:
        return self._sync.url

    @property
    def token(self) -> str | None:
        return self._sync.token

    async def authenticate(self) -> str:
        return await asyncio.to_thread(self._sync.authenticate)

    async def request(self, method: str, path: str, **kw) -> dict:
        return await asyncio.to_thread(self._sync.request, method, path, **kw)

    async def list_page(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.list_page, *args, **kwargs)

    async def list_all(self, *args, **kwargs) -> list[dict]:
        return await asyncio.to_thread(self._sync.list_all, *args, **kwargs)

    async def get_record(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.get_record, *args, **kwargs)

    async def create_record(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.create_record, *args, **kwargs)

    async def update_record(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.update_record, *args, **kwargs)

    async def delete_record(self, *args, **kwargs) -> None:
        await asyncio.to_thread(self._sync.delete_record, *args, **kwargs)

    async def list_collections(self) -> list[dict]:
        return await asyncio.to_thread(self._sync.list_collections)

    async def get_collection(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.get_collection, *args, **kwargs)

    async def create_collection(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.create_collection, *args, **kwargs)

    async def update_collection(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self._sync.update_collection, *args, **kwargs)

    async def delete_collection(self, *args, **kwargs) -> None:
        await asyncio.to_thread(self._sync.delete_collection, *args, **kwargs)
