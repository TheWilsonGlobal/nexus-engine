"""Run async provider/network code from the synchronous agent loop on ONE long-lived event loop.

Cached httpx.AsyncClient instances are bound to the loop they were first used on, so a fresh
asyncio.run() per call breaks every call after the first ("Event loop is closed"). All sync->async
calls go through this single background loop instead.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, AsyncIterator, Awaitable, Iterator, Optional, TypeVar

T = TypeVar("T")

_loop: Optional[asyncio.AbstractEventLoop] = None
_lock = threading.Lock()


def background_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="nexus-async-bridge", daemon=True).start()
        return _loop


def run_async(coro: Awaitable[T], timeout: Optional[float] = None) -> T:
    """Block the calling (non-loop) thread until `coro` finishes on the background loop."""
    fut = asyncio.run_coroutine_threadsafe(coro, background_loop())  # type: ignore[arg-type]
    try:
        return fut.result(timeout)
    except BaseException:
        fut.cancel()
        raise


_DONE = object()


def iter_async(agen: AsyncIterator[T]) -> Iterator[T]:
    """Consume an async iterator from sync code, item by item, as it produces them.

    Closing the returned generator early (e.g. the SSE client disconnected) cancels the producer.
    """
    q: "queue.Queue[tuple[Any, Any]]" = queue.Queue()

    async def pump() -> None:
        try:
            async for item in agen:
                q.put((True, item))
            q.put((_DONE, None))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the consumer side
            q.put((False, exc))

    fut = asyncio.run_coroutine_threadsafe(pump(), background_loop())
    try:
        while True:
            tag, item = q.get()
            if tag is _DONE:
                return
            if tag is False:
                raise item
            yield item
    finally:
        fut.cancel()
