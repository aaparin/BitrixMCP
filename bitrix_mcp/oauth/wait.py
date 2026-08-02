from __future__ import annotations

import asyncio


class AuthWaitRegistry:
    """In-memory waiters keyed by OAuth state nonce."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def register(self, state: str) -> asyncio.Event:
        async with self._lock:
            event = self._events.get(state)
            if event is None:
                event = asyncio.Event()
                self._events[state] = event
            return event

    async def signal(self, state: str) -> bool:
        async with self._lock:
            event = self._events.pop(state, None)
        if event is None:
            return False
        event.set()
        return True

    async def discard(self, state: str) -> None:
        async with self._lock:
            self._events.pop(state, None)

    @property
    def pending_count(self) -> int:
        return len(self._events)
