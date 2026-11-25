"""Rate limits for the HTTP services (24.32): the intake API, the MCP server and the
approvals page.

- `Window`: at most `limit` events per key in the last `seconds`, in this process. The
  services run one to three pods (13.4's HPA), so a limit holds per pod: the cluster-wide
  ceiling is that times the pod count. A restart forgets the counts.
- `FailedAuthLimit`: ASGI middleware that counts the 401 answers (no key, or a key or
  token that matches nobody) each client address gets. Past `limit` in a minute, that
  address gets 429 with `Retry-After` for every request, before the app reads it: guessing
  keys or tokens is slowed to a crawl, and a client with a good key is never counted.

Every server also caps its open connections (uvicorn `limit_concurrency`: 503 past it).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any


class Window:
    """A sliding window per key. `hit` records an event and says whether it is allowed;
    `retry_after` is the seconds until the oldest counted event leaves the window."""

    def __init__(self, limit: int, seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000):  # fmt: skip
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit, self.seconds, self.clock, self.max_keys = limit, seconds, clock, max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        """The key's events still in the window; a key never seen is not stored."""
        q = self._events.get(key)
        if q is None:
            return deque()
        while q and q[0] <= now - self.seconds:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._trim(key, self.clock())) >= self.limit

    def hit(self, key: str) -> bool:
        with self._lock:
            now = self.clock()
            if key not in self._events and len(self._events) >= self.max_keys:
                # Bounded memory: drop keys whose events have all expired, then the oldest.
                for k in [
                    k for k, q in self._events.items() if not q or q[-1] <= now - self.seconds
                ]:
                    del self._events[k]
                if len(self._events) >= self.max_keys:
                    del self._events[next(iter(self._events))]
            q = self._trim(key, now)
            if len(q) >= self.limit:
                return False
            q.append(now)
            self._events[key] = q
            return True

    def retry_after(self, key: str) -> int:
        with self._lock:
            q = self._trim(key, self.clock())
            if len(q) < self.limit or not q:
                return 0
            return max(1, math.ceil(q[0] + self.seconds - self.clock()))


def _client_address(scope: dict[str, Any]) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


class FailedAuthLimit:
    """ASGI middleware: an address that got `limit` 401 answers in the last minute gets 429
    for every request until the oldest of them is a minute old. Behind a NodePort, kind's
    clients can share the node's address, so a guesser there can lock out the others for a
    minute: the cost of slowing it down on a laptop cluster."""

    def __init__(self, app: Any, limit: int = 20, window: Window | None = None):
        self.app = app
        self.window = window or Window(limit, 60.0)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        addr = _client_address(scope)
        if self.window.blocked(addr):
            wait = str(self.window.retry_after(addr) or 60)
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"retry-after", wait.encode())]})  # fmt: skip
            await send({"type": "http.response.body", "body": b"too many failed attempts\n"})
            return

        async def counting_send(message: dict) -> None:
            if message["type"] == "http.response.start" and message["status"] == 401:
                self.window.hit(addr)
            await send(message)

        await self.app(scope, receive, counting_send)
