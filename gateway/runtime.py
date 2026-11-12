"""Per-session runtime: one turn at a time, events fanned out to every open client.

Text and voice share a session's runtime, so a spoken question shows up in the
chat window and an action card raised by voice can be confirmed by clicking.
Starting a new turn while one is running cancels the old one: that is barge-in
for voice, and "the customer changed their mind" for text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from core.assistant import Assistant
from core.session import Session

log = logging.getLogger("gateway.runtime")


class SessionRuntime:
    def __init__(self, assistant: Assistant, session: Session):
        self.assistant, self.session = assistant, session
        self.listeners: set = set()  # WebSockets
        self.voice_sinks: list[Callable[[dict], Awaitable[None]]] = []
        self.task: asyncio.Task | None = None

    async def emit(self, event: dict) -> None:
        dead = []
        for ws in list(self.listeners):
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001 - a closed tab must not break the turn
                dead.append(ws)
        for ws in dead:
            self.listeners.discard(ws)
        for sink in list(self.voice_sinks):
            try:
                await sink(event)
            except Exception:  # noqa: BLE001
                log.exception("voice sink failed")

    async def cancel(self) -> bool:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await self.emit({"type": "cancelled"})
            return True
        return False

    def _run(self, coro_factory, cancel_previous: bool = True) -> asyncio.Task:
        previous = self.task

        async def runner():
            if not cancel_previous and previous and not previous.done():
                try:  # let the reply in progress finish first
                    await previous
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            try:
                await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("turn failed")
                await self.emit(
                    {"type": "error", "text": f"Something went wrong: {type(e).__name__}"}
                )
            finally:
                await self.assistant.sessions.save(self.session)

        if cancel_previous and previous and not previous.done():
            previous.cancel()
        self.task = asyncio.create_task(runner())
        return self.task

    def start_turn(self, text: str, channel: str = "text") -> asyncio.Task:
        async def turn():
            await self.emit({"type": "user", "text": text, "channel": channel})
            await self.assistant.orchestrator.handle(self.session, text, self.emit, channel=channel)

        return self._run(turn)

    def start_action(self, fn: Callable[[Callable], Awaitable[str]]) -> asyncio.Task:
        async def act():
            say = await fn(self.emit)
            self.session.add_turn("(confirmation)", say)
            await self.emit(
                {
                    "type": "done",
                    "text": say,
                    "intent": None,
                    "pack": None,
                    "verified": True,
                    "unverified_numbers": [],
                    "explain": {"tools": [], "rules": [], "clauses": [], "verified": True},
                    "trace": None,
                }
            )

        return self._run(act, cancel_previous=False)


class Runtimes:
    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        self._by_session: dict[str, SessionRuntime] = {}

    def get(self, session: Session) -> SessionRuntime:
        rt = self._by_session.get(session.session_id)
        if rt is None:
            rt = self._by_session[session.session_id] = SessionRuntime(self.assistant, session)
        return rt
