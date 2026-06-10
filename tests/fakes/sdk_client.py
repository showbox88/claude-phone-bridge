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
        # Phase 6a Task 5: structured queue used by run_user_turn tests.
        # Kept separate from scripted_response so existing tests (which
        # leave both empty) keep working.
        self._queued_blocks: list[Any] = []
        self._queued_result: Any = None
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
        # Legacy path: scripted_response (raw items / exceptions).
        for item in self.scripted_response:
            if isinstance(item, BaseException):
                raise item
            await asyncio.sleep(0)
            yield item
        # Phase 6a Task 5: structured queue (blocks then terminating result).
        for blk in self._queued_blocks:
            await asyncio.sleep(0)
            yield blk
        if self._queued_result is not None:
            await asyncio.sleep(0)
            yield self._queued_result

    # ---- test helpers (Phase 6a Task 5) ----------------------------------

    def queue_assistant_text(self, text: str) -> None:
        """Queue an AssistantMessage with a single TextBlock."""
        from claude_agent_sdk import AssistantMessage, TextBlock
        self._queued_blocks.append(
            AssistantMessage(content=[TextBlock(text=text)], model="fake-model")
        )

    def queue_result(self, *, cost_usd: float = 0.0,
                     input_tokens: int = 0, output_tokens: int = 0,
                     duration_ms: int = 0, num_turns: int = 1,
                     session_id: str = "fake-sdk-sess") -> None:
        """Queue the terminating ResultMessage."""
        from claude_agent_sdk import ResultMessage
        self._queued_result = ResultMessage(
            subtype="success",
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            is_error=False,
            num_turns=num_turns,
            session_id=session_id,
            total_cost_usd=cost_usd,
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        )

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
