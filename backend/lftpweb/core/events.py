"""In-process event bus: `core/engine.py` publishes model changes, `api/ws.py` fans them out
to every connected browser (DESIGN.md §2, §9). Deliberately tiny — one topic, JSON-serializable
dict messages, no persistence (that's the `event` DB table, a separate and unrelated concept
despite the name overlap — see DESIGN.md §3.1/§10.1 vs. this module).
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    """Fan-out from one publisher to N subscribers, each with its own bounded queue so one
    slow WebSocket client can't block scanning or other clients. A full queue drops the
    oldest pending message for that subscriber rather than blocking `publish()` — a dropped
    delta is recovered by the next one (the model is idempotent state, not a diff log), so
    losing one is harmless; blocking the engine loop on a stuck browser tab is not.
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    pass  # give up silently; the next publish will retry
