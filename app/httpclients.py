"""Long-lived outbound httpx clients, bound to the event loop that built them.

An httpx.AsyncClient pools connections, and a pooled connection belongs to the
event loop that opened it. uvicorn runs one loop per process, so a client made
on first use and closed at shutdown is correct in production. But a process
that runs the app's lifespan more than once runs each on a NEW loop -- the test
suite does, and so would anything embedding the app -- and a client carried over
from an earlier loop can neither send on the new one nor be closed from it.

So each client here is built lazily, remembers the loop it was built on, and is
REPLACED rather than reused when asked for from a different loop. The lifespan
closes every one at shutdown (close_all), on the loop that built it.

A client abandoned because its loop is gone is dropped, not closed: closing it
would have to run on that loop, which no longer exists. That can only happen in
a process that runs more than one loop, never under uvicorn.

Nothing here changes how any client is configured. Each holder is given a
factory, and the factory is the exact constructor call its module made before.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Callable

import httpx

log = logging.getLogger("xhc.httpclients")

# Every holder, for close_all() and for the test that no client outlives its
# lifespan. Weak: an OIDCClient built in a test must not be kept alive by this.
_holders: weakref.WeakSet[LoopBound] = weakref.WeakSet()


class LoopBound:
    """One lazily built httpx.AsyncClient, tied to the loop that built it."""

    def __init__(self, name: str, factory: Callable[[], httpx.AsyncClient]) -> None:
        self.name = name
        self._factory = factory
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        _holders.add(self)

    @property
    def current(self) -> httpx.AsyncClient | None:
        """The client as it stands, without building one. For tests and status."""
        return self._client

    def get(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is not None and self._loop is not loop:
            log.debug("%s: client built on another event loop; replacing it", self.name)
            self._client = None
        if self._client is None:
            self._client = self._factory()
            self._loop = loop
        return self._client

    async def aclose(self) -> None:
        client, loop = self._client, self._loop
        self._client = self._loop = None
        if client is None or client.is_closed:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is running:
            await client.aclose()
        else:
            # Its connections belong to a loop that is not this one; closing
            # them from here is what raised "Event loop is closed".
            log.debug("%s: dropping a client from another event loop unclosed", self.name)


async def close_all() -> None:
    """Close every holder's client. Called from the lifespan's shutdown."""
    for holder in list(_holders):
        try:
            await holder.aclose()
        except Exception:
            # One client failing to close must not stop the rest closing.
            log.exception("closing the %s HTTP client failed", holder.name)


def open_clients() -> list[tuple[str, httpx.AsyncClient]]:
    """Every holder's client that is built and not closed."""
    return [
        (h.name, h.current) for h in list(_holders)
        if h.current is not None and not h.current.is_closed
    ]
