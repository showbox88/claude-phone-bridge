"""FakeClient: in-memory stand-in for ClaudeSDKClient.

Records every method call so tests can assert ordering, lets tests inject
scripted response streams. Not a perfect mock — covers only what
SessionManager / run_user_turn actually use."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, options: Any = None):
        self.options = options
        self.connected = False
        self.disconnect_called = False
        self.queries: list[Any] = []
        self.scripted_response: list[Any] = []
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_called = True
        self.connected = False

    async def query(self, msg_iter: AsyncIterator[Any]) -> None:
        async for msg in msg_iter:
            self.queries.append(msg)

    async def receive_response(self) -> AsyncIterator[Any]:
        for item in self.scripted_response:
            if isinstance(item, BaseException):
                raise item
            await asyncio.sleep(0)
            yield item

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
