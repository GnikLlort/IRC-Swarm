"""Assorted asyncio helpers shared across the manager.

Home of the asynchronous :class:`EventEmitter`, the per-bot
:class:`TokenBucket` rate limiter and small formatting utilities.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List

log = logging.getLogger("irc_bot_manager")


class EventEmitter:
    """A tiny async publish/subscribe bus.

    Handlers are async callables.  Emitting runs every subscribed handler
    concurrently via :func:`asyncio.gather` so a slow or failing handler never
    blocks (or crashes) the others or the emitting task.
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Callable[..., Awaitable[None]]]] = defaultdict(list)

    def on(self, event: str, handler: Callable[..., Awaitable[None]]) -> None:
        if handler not in self._handlers[event]:
            self._handlers[event].append(handler)

    def off(self, event: str, handler: Callable[..., Awaitable[None]]) -> None:
        try:
            self._handlers[event].remove(handler)
        except ValueError:
            pass

    def clear(self, event: str | None = None) -> None:
        if event is None:
            self._handlers.clear()
        else:
            self._handlers.pop(event, None)

    async def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        handlers = list(self._handlers.get(event, []))
        if not handlers:
            return
        async def _run(h) -> None:
            try:
                res = h(*args, **kwargs)
                if asyncio.iscoroutine(res) or asyncio.isfuture(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - handler isolation by design
                log.exception("Event handler for %r raised", event)
        await asyncio.gather(*(_run(h) for h in handlers), return_exceptions=False)


class TokenBucket:
    """Asyncio friendly token bucket used to throttle one bot's outbound chat.

    Each bot owns its own bucket instance so throttling one bot never delays
    another.  ``rate`` is tokens/second and ``burst`` is the bucket capacity.
    """

    def __init__(self, rate: float, burst: float, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.rate = max(float(rate), 1e-6)
        self.capacity = max(float(burst), 1.0)
        self._tokens = float(self.capacity)
        self._ts = time.monotonic()
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._ts
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._ts = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available then consume them."""
        tokens = max(tokens, 0.0)
        if tokens == 0:
            return
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    def notify(self) -> None:
        """Wake any waiting acquirer after external refills (optional)."""
        self._wakeup.set()

    @property
    def available(self) -> float:
        """Approximate current tokens (unsynchronised, for display only)."""
        self._refill()
        return self._tokens


def format_duration(seconds: float) -> str:
    """Render seconds as a compact ``Xd Xh Xm Xs`` duration string."""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def redact_url(url: str) -> str:
    """Return a copy of ``scheme://user:pass@host`` with credentials stripped."""
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if "@" in rest:
        _, _, hostport = rest.rpartition("@")
        rest = hostport
    return f"{scheme}://{rest}"


def safe_nick_suffix() -> str:
    """Small random suffix appended when a nick collides."""
    import random
    return str(random.randint(10, 9999))
