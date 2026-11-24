"""Update routing: fan TDLib's unsolicited updates out to subscribers.

TDLib delivers every unsolicited update (new messages, callback queries,
file progress, authorization changes, …) through the client's receive loop.
This module turns that single stream into a small pub/sub router:

* subscribers register for explicit ``update*`` type names (or ``None`` for
  everything);
* callbacks run on the asyncio event loop thread;
* callbacks may be plain functions or coroutine functions — coroutine
  results become **tracked** tasks so an exception in one handler can never
  become an unhandled task failure or silently kill the router.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import itertools
from typing import Any, Callable, Iterable

log = logging.getLogger("copybot.tdlib.updates")

__all__ = ["UpdateRouter", "TrackedTasks"]

Callback = Callable[[dict], Any]


class TrackedTasks:
    """Registry of background tasks: tracked, logged, cancellable, drainable."""

    def __init__(self, name: str = "tasks"):
        self._name = name
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("unhandled exception in %s task %s: %r",
                      self._name, task.get_name(), exc)

    @property
    def active(self) -> set[asyncio.Task]:
        return set(self._tasks)

    def cancel_all(self) -> int:
        n = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                n += 1
        return n

    async def drain(self, timeout: float) -> bool:
        """Wait (bounded) for all tasks to finish. True == all done."""
        if not self._tasks:
            return True
        done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        if pending:
            for t in pending:
                t.cancel()
            # second, shorter wait for the cancellations to take effect
            await asyncio.wait(pending, timeout=min(5.0, timeout))
            return False
        return True

    def __len__(self) -> int:
        return len(self._tasks)


class UpdateRouter:
    """Fan-out router for TDLib updates (single event-loop thread)."""

    def __init__(self, name: str = "router"):
        self._name = name
        self._subs: dict[int, tuple[frozenset[str] | None, Callback]] = {}
        self._counter = itertools.count()
        self.tasks = TrackedTasks(f"updates:{name}")

    # -- subscription ----------------------------------------------------
    def subscribe(self, callback: Callback,
                  types: Iterable[str] | None = None) -> int:
        """Register *callback* for *types* (None ⇒ every update)."""
        key = next(self._counter)
        tset = frozenset(types) if types is not None else None
        self._subs[key] = (tset, callback)
        return key

    def unsubscribe(self, key: int) -> None:
        self._subs.pop(key, None)

    # -- dispatch (called on the event loop thread) ----------------------
    def dispatch(self, payload: dict) -> None:
        utype = payload.get("@type", "")
        for tset, cb in list(self._subs.values()):
            if tset is not None and utype not in tset:
                continue
            try:
                res = cb(payload)
            except Exception:
                log.exception("update subscriber failed for %s", utype)
                continue
            if inspect.isawaitable(res):
                self.tasks.spawn(self._await_cb(res, cb, utype),
                                 name=f"cb:{utype}")

    @staticmethod
    async def _await_cb(awaitable, cb, utype: str) -> None:
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("async update subscriber failed for %s", utype)

    async def shutdown(self, timeout: float = 10.0) -> None:
        self.tasks.cancel_all()
        await self.tasks.drain(timeout)
