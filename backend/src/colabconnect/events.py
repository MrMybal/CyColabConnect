"""In-process pub/sub used to push live updates to the web UI."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Any, Iterator


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, type_: str, **data: Any) -> None:
        event = {"type": type_, "ts": time.time(), **data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the event rather than block producers.
                pass

    @contextmanager
    def subscribe(self, maxsize: int = 1000) -> Iterator[asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)


bus = EventBus()
