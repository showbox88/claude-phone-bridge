"""SessionManager — per-session ClaudeAgent registry.

Lazily constructs a ClaudeAgent for each active session_id. Recreate
(model/cwd switch) holds the agent's turn_lock so it never tears down
an in-flight turn. Destroy disconnects the SDK client cleanly.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from app.agent.agent import ClaudeAgent
from app.log import get_logger

log = get_logger("bridge")


class SessionManager:
    def __init__(self) -> None:
        self._agents: dict[str, ClaudeAgent] = {}

    def get(self, sid: str) -> ClaudeAgent | None:
        return self._agents.get(sid)

    async def get_or_create(self, sid: str, *, cwd: Path,
                            mode: str = "code", model: str = "",
                            sdk_session_id: str | None = None) -> ClaudeAgent:
        """Return existing agent for sid; create + connect if absent.

        Race safety: the new agent is inserted into the registry BEFORE
        awaiting `_connect`. A concurrent caller for the same sid will
        find the partially-initialized agent (client=None) and return
        it instead of constructing a parallel client. If `_connect`
        fails, the slot is rolled back and the exception propagates.
        """
        existing = self._agents.get(sid)
        if existing is not None:
            return existing
        agent = ClaudeAgent(
            session_id=sid, cwd=cwd, mode=mode, model=model,
            sdk_session_id=sdk_session_id,
        )
        # Reserve slot before the await — prevents the second concurrent
        # call from building a parallel client.
        self._agents[sid] = agent
        try:
            await self._connect(agent)
        except BaseException:
            # Roll back so a retry doesn't see a half-dead agent.
            self._agents.pop(sid, None)
            raise
        return agent

    async def _connect(self, agent: ClaudeAgent) -> None:
        # Delayed import to avoid pulling claude_agent_sdk at module load
        # time (it spawns the bundled CLI on first import).
        from claude_agent_sdk import ClaudeSDKClient
        from app.agent.options import make_options

        log.info("agent connect sid=%s mode=%s model=%s cwd=%s",
                 agent.session_id, agent.mode, agent.model or "default",
                 agent.cwd)
        agent.client = ClaudeSDKClient(options=make_options(agent))
        await agent.client.connect()

    async def recreate(self, sid: str, *, cwd: Path | None = None,
                       mode: str | None = None,
                       model: str | None = None,
                       sdk_session_id: str | None = None) -> ClaudeAgent:
        """Tear down + reconnect this session's client without affecting
        others. Waits for any in-flight turn to finish (holds turn_lock)."""
        agent = self._agents.get(sid)
        if agent is None:
            raise KeyError(f"no agent for session {sid}")
        async with agent.turn_lock:
            if agent.client is not None:
                with contextlib.suppress(Exception):
                    await agent.client.disconnect()
                agent.client = None
            if cwd is not None: agent.cwd = cwd
            if mode is not None: agent.mode = mode
            if model is not None: agent.model = model
            if sdk_session_id is not None: agent.sdk_session_id = sdk_session_id
            await self._connect(agent)
        return agent

    async def destroy(self, sid: str) -> None:
        agent = self._agents.pop(sid, None)
        if agent is None:
            return
        if agent.current_turn_task and not agent.current_turn_task.done():
            agent.current_turn_task.cancel()
            with contextlib.suppress(BaseException):
                await agent.current_turn_task
        if agent.client is not None:
            with contextlib.suppress(Exception):
                await agent.client.disconnect()

    async def shutdown(self) -> None:
        sids = list(self._agents.keys())
        for sid in sids:
            await self.destroy(sid)

    def active_ids(self) -> list[str]:
        return list(self._agents.keys())


manager: SessionManager = SessionManager()
