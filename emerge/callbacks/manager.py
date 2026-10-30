import asyncio
import collections
import dataclasses
import logging
from typing import Protocol

from emerge.callbacks.base import CallbackHandler
from emerge.events import Event

logger = logging.getLogger(__name__)

_STOP = object()


class EventEmitter(Protocol):
    async def emit(self, event: Event) -> None: ...

    def emit_nowait(self, event: Event) -> None: ...

    def with_event_defaults(
        self, *, run_id: str | None = None, node_id: str | None = None, irreversible: bool | None = None
    ) -> "CallbackView": ...


class _Worker:
    def __init__(self, handler: CallbackHandler) -> None:
        self.handler = handler
        self.queue: asyncio.Queue[Event | object] = asyncio.Queue()
        self.task = asyncio.create_task(self._consume(), name=f"callback:{type(handler).__name__}")

    async def _consume(self) -> None:
        while True:
            item = await self.queue.get()
            if item is _STOP:
                break
            try:
                await self.handler.on_event(item)  # type: ignore[arg-type]
            except Exception:
                logger.exception("callback handler %r failed on %s", self.handler, item.type)  # type: ignore[union-attr]
        try:
            await self.handler.close()
        except Exception:
            logger.exception("callback handler %r failed to close", self.handler)


class CallbackManager:
    """Fan events out to handlers; each handler has its own queue and task, so emit only enqueues."""

    def __init__(self) -> None:
        self._workers: dict[int, _Worker] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        # Events from other threads wait here so they keep their order relative to loop-thread emits.
        self._pending: collections.deque[Event] = collections.deque()

    def add_handler(self, handler: CallbackHandler) -> None:
        self._bind_loop()
        if id(handler) not in self._workers:
            self._workers[id(handler)] = _Worker(handler)

    def remove_handler(self, handler: CallbackHandler) -> None:
        worker = self._workers.pop(id(handler), None)
        if worker is not None:
            worker.queue.put_nowait(_STOP)

    async def emit(self, event: Event) -> None:
        self.emit_nowait(event)

    def emit_nowait(self, event: Event) -> None:
        """Safe to call from sync code on the loop thread or from any other thread."""
        loop = self._loop
        if loop is None:
            raise RuntimeError("CallbackManager has no event loop yet; add a handler from within the loop first")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._flush_pending()
            self._dispatch(event)
        else:
            self._pending.append(event)
            loop.call_soon_threadsafe(self._flush_pending)

    def with_event_defaults(
        self, *, run_id: str | None = None, node_id: str | None = None, irreversible: bool | None = None
    ) -> "CallbackView":
        return CallbackView(self, {"run_id": run_id, "node_id": node_id, "irreversible": irreversible})

    async def aclose(self) -> None:
        self._flush_pending()
        self._closed = True
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            worker.queue.put_nowait(_STOP)
        await asyncio.gather(*(worker.task for worker in workers))

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("CallbackManager is bound to a different event loop")

    def _flush_pending(self) -> None:
        while self._pending:
            self._dispatch(self._pending.popleft())

    def _dispatch(self, event: Event) -> None:
        if self._closed:
            logger.warning("dropping %s event emitted after CallbackManager was closed", event.type)
            return
        for worker in self._workers.values():
            if worker.handler.accepts(event):
                worker.queue.put_nowait(event)


class CallbackView:
    """Emits through a manager, filling run_id / node_id / irreversible where the event leaves them empty."""

    def __init__(self, manager: CallbackManager, defaults: dict[str, str | bool | None]) -> None:
        self._manager = manager
        self._defaults = {k: v for k, v in defaults.items() if v is not None}

    async def emit(self, event: Event) -> None:
        self._manager.emit_nowait(self._fill(event))

    def emit_nowait(self, event: Event) -> None:
        self._manager.emit_nowait(self._fill(event))

    def with_event_defaults(
        self, *, run_id: str | None = None, node_id: str | None = None, irreversible: bool | None = None
    ) -> "CallbackView":
        overrides = {"run_id": run_id, "node_id": node_id, "irreversible": irreversible}
        merged = self._defaults | {k: v for k, v in overrides.items() if v is not None}
        return CallbackView(self._manager, merged)

    def _fill(self, event: Event) -> Event:
        missing = {k: v for k, v in self._defaults.items() if getattr(event, k) is None}
        return dataclasses.replace(event, **missing) if missing else event
