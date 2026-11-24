"""Async TDLib client: request dispatch, update routing, lifecycle.

Architecture
------------
Each :class:`TDClinet` owns:

* one opaque ``libtdjson`` handle (``td_json_client_create``),
* one dedicated **receive thread** that blocks in ``td_json_client_receive``
  with a short timeout (not a busy loop) and hops every payload onto the
  asyncio event loop via ``call_soon_threadsafe``,
* a dict of pending futures keyed by a unique ``@extra`` correlation id,
* an :class:`~tdlib_updates.UpdateRouter` for unsolicited updates.

Guarantees
----------
* concurrent requests are safe — each carries a unique ``@extra``;
* responses that arrive after a timeout are dropped (no future leaks);
* request timeouts raise :class:`~errors.TdTimeoutError`;
* TDLib ``error`` objects raise :class:`~errors.TdError`;
* ``close()`` is idempotent, bounded in time, and never hangs;
* several client instances (user + bot) run side by side, each with its
  own receive thread and database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Iterable

from errors import TdClientClosedError, TdError, TdTimeoutError
from tdlib_native import NativeTdLib
from tdlib_updates import UpdateRouter

log = logging.getLogger("copybot.tdlib.client")

__all__ = ["TDClient", "UpdateWaiter"]

AUTH_READY = "authorizationStateReady"
AUTH_CLOSED = "authorizationStateClosed"


class UpdateWaiter:
    """One-shot subscription to a future update (subscribe-before-send)."""

    def __init__(self, client: "TDClient", types: Iterable[str] | None,
                 predicate: Callable[[dict], bool] | None = None):
        self._client = client
        self._predicate = predicate
        self._future: asyncio.Future = client.loop.create_future()
        self._key = client.router.subscribe(self._on_update, types)

    def _on_update(self, payload: dict) -> None:
        if self._future.done():
            return
        try:
            if self._predicate is not None and not self._predicate(payload):
                return
        except Exception:
            log.exception("update predicate failed")
            return
        self._future.set_result(payload)

    async def wait(self, timeout: float | None = None) -> dict:
        try:
            if timeout is None:
                return await self._future
            return await asyncio.wait_for(self._future, timeout)
        finally:
            self._client.router.unsubscribe(self._key)

    def cancel(self) -> None:
        self._client.router.unsubscribe(self._key)
        if not self._future.done():
            self._future.cancel()


class TDClient:
    """A single TDLib instance bound to one account (user or bot)."""

    def __init__(self, name: str, native: NativeTdLib, *,
                 loop: asyncio.AbstractEventLoop | None = None,
                 router: UpdateRouter | None = None,
                 request_timeout: float = 60.0,
                 send_timeout: float = 1800.0):
        self.name = name
        self._native = native
        self.loop = loop or asyncio.get_running_loop()
        self.router = router or UpdateRouter(name)
        self._default_timeout = request_timeout
        self._send_timeout = send_timeout

        self._handle: int = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._closing = False
        self._pending: dict[str, asyncio.Future] = {}

        self.authorization_state: str = "authorizationStateNone"
        self._recv_count = 0
        self._send_count = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Create the native handle and spawn the receive thread."""
        if self._handle:
            raise RuntimeError(f"client {self.name!r} already started")
        self._handle = self._native.create_client()
        self._stop.clear()
        self._closed.clear()
        self._thread = threading.Thread(
            target=self._receive_loop, name=f"tdlib-recv-{self.name}",
            daemon=True)
        self._thread.start()
        log.info("TDLib client %r started (handle=%d)", self.name, self._handle)

    @property
    def started(self) -> bool:
        return bool(self._handle)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def is_ready(self) -> bool:
        return self.authorization_state == AUTH_READY

    async def close(self, timeout: float = 10.0) -> None:
        """Graceful, bounded shutdown: close → wait → join → destroy."""
        if self._closing:
            # A concurrent close() caller: wait for the first one to finish.
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._closed.wait(timeout))
            return
        if not self._handle:
            self._closed.set()
            return
        self._closing = True
        log.info("closing TDLib client %r …", self.name)
        try:
            self._native.send(self._handle, {"@type": "close"})
        except Exception as exc:  # handle already dying — fall through
            log.debug("close request failed: %s", exc)

        # The receive thread stays up until authorizationStateClosed arrives
        # (or the deadline expires) so TDLib can flush its databases.
        deadline = time.monotonic() + timeout
        while not self._closed.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, min(2.0, timeout))
            self._thread = None

        handle, self._handle = self._handle, 0
        try:
            await asyncio.to_thread(self._native.destroy_client, handle)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("destroy_client(%s) failed: %s", handle, exc)

        err = TdClientClosedError(f"client {self.name!r} is closed")
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()
        self._closed.set()
        log.info("TDLib client %r closed", self.name)

    # ------------------------------------------------------------------
    # receive thread (the only thread that may call receive() on the handle)
    # ------------------------------------------------------------------
    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._native.receive(self._handle, 0.25)
            except Exception as exc:
                if self._stop.is_set():
                    break
                log.error("td_receive failed: %s", exc)
                time.sleep(0.25)   # avoid a hot spin on persistent errors
                continue
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                log.warning("unparseable TDLib payload: %.200s", raw)
                continue
            try:
                self.loop.call_soon_threadsafe(self._on_payload, payload)
            except RuntimeError:
                break   # event loop is closed — nothing left to do

    # ------------------------------------------------------------------
    # event-loop side
    # ------------------------------------------------------------------
    def _on_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        self._recv_count += 1
        extra = payload.get("@extra")
        if extra is not None:
            fut = self._pending.pop(str(extra), None)
            if fut is None:
                log.debug("dropping unmatched response @extra=%s type=%s",
                          extra, payload.get("@type"))
                return
            if fut.done():
                return
            if payload.get("@type") == "error":
                fut.set_exception(TdError(int(payload.get("code", 0)),
                                          str(payload.get("message", ""))))
            else:
                fut.set_result(payload)
            return

        utype = payload.get("@type", "")
        if utype == "updateAuthorizationState":
            state = (payload.get("authorization_state") or {}).get("@type", "")
            self.authorization_state = state
            log.debug("client %s auth state → %s", self.name, state)
            if state == AUTH_CLOSED:
                self._closed.set()
        self.router.dispatch(payload)

    # ------------------------------------------------------------------
    # request API
    # ------------------------------------------------------------------
    async def request(self, payload: dict,
                      timeout: float | None = None) -> dict:
        """Send *payload* and await its correlated response."""
        if not self._handle or self._closing:
            raise TdClientClosedError(f"client {self.name!r} is closed")
        if timeout is None:
            timeout = self._default_timeout
        extra = f"{self.name}:{uuid.uuid4().hex}"
        fut: asyncio.Future = self.loop.create_future()
        self._pending[extra] = fut
        req = dict(payload)
        req["@extra"] = extra
        try:
            self._native.send(self._handle, req)
            self._send_count += 1
        except Exception as exc:
            self._pending.pop(extra, None)
            raise TdError(-1, f"failed to queue request: {exc}") from exc
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(extra, None)
            raise TdTimeoutError(
                f"{self.name}: {payload.get('@type')} timed out "
                f"after {timeout:.0f}s") from None
        except asyncio.CancelledError:
            self._pending.pop(extra, None)
            raise

    # convenience for long operations (uploads / large sends)
    async def request_long(self, payload: dict,
                           timeout: float | None = None) -> dict:
        return await self.request(
            payload, timeout if timeout is not None else self._send_timeout)

    def expect(self, types: Iterable[str] | None = None,
               predicate: Callable[[dict], bool] | None = None) -> UpdateWaiter:
        """Create a one-shot update subscription (subscribe BEFORE sending)."""
        return UpdateWaiter(self, types, predicate)

    async def drain_responses(self, timeout: float = 0.1) -> None:
        """Give in-flight responses a moment to arrive (tests/shutdown)."""
        await asyncio.sleep(timeout)

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "auth_state": self.authorization_state,
            "requests_sent": self._send_count,
            "payloads_received": self._recv_count,
            "pending": len(self._pending),
            "closed": self._closed.is_set(),
        }
