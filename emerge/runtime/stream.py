"""Run output for outside callers to pull, by mode.

Callbacks hand events to the framework's own components; a stream is what an outside caller (the
TUI, a headless script) reads at its own pace. `StreamHandler` is one more callback handler that
puts each event on the queue of every subscriber whose modes include it:

- `messages`: model tokens (`model.token`, with `kind` and `part`)
- `updates`: new port messages and node state changes
- `values`: a snapshot of the whole run (topology, node states, latest value of every port), as a
  `stream.values` event, at subscription and after every update
- `custom`: what nodes push with `ctx.emit_event`
- `debug`: every event
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal, get_args

from emerge.callbacks import CallbackHandler
from emerge.events import Event, EventType
from emerge.exceptions import RuntimeControlError

StreamMode = Literal["messages", "updates", "values", "custom", "debug"]
STREAM_MODES: frozenset[str] = frozenset(get_args(StreamMode))
# The type of the snapshot events a `values` subscriber receives.
VALUES_EVENT = "stream.values"

_TYPES: dict[str, frozenset[str]] = {
    "messages": frozenset({EventType.MODEL_TOKEN}),
    "updates": frozenset({EventType.PORT_PUBLISHED, EventType.NODE_STATE}),
    "custom": frozenset({EventType.NODE_CUSTOM}),
}
# After these a values subscriber gets a fresh snapshot.
_CHANGES_VALUES = _TYPES["updates"] | {EventType.GRAPH_PATCHED}

# Snapshot of one run: {"graph_version", "graph" (the graph file form), "nodes", "ports"}.
ValuesProvider = Callable[[str], Awaitable[dict[str, Any]]]

_END = object()


class Subscription:
    """One subscriber's queue. Iterate it; `aclose()` (or leaving `async with`) unsubscribes."""

    def __init__(self, handler: StreamHandler, modes: frozenset[str], run_id: str | None) -> None:
        self.modes = modes
        self.run_id = run_id
        self._handler: StreamHandler | None = handler
        self._queue: asyncio.Queue[Any] | None = asyncio.Queue()
        self._primed = "values" not in modes or run_id is None

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        if self._queue is None:
            raise StopAsyncIteration
        if not self._primed:
            # A values subscriber starts from the full picture, then follows the changes.
            self._primed = True
            assert self._handler is not None and self.run_id is not None
            snapshot = await self._handler.snapshot(self.run_id)
            if snapshot is not None:
                return snapshot
        item = await self._queue.get()
        if item is _END:
            self._release()
            raise StopAsyncIteration
        return item

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._release()

    def put(self, item: Any) -> None:
        if self._queue is not None:
            self._queue.put_nowait(item)

    def _release(self) -> None:
        if self._handler is not None:
            self._handler._drop(self)
            self._handler = None
        self._queue = None

    def __del__(self) -> None:
        # Dropped without aclose(), e.g. after `break` out of the loop: stop queueing for it.
        handler = self._handler
        if handler is not None:
            handler._drop(self)


class StreamHandler(CallbackHandler):
    """Fans events out to subscribers by mode; any number of subscribers at once."""

    def __init__(self, values: ValuesProvider | None = None) -> None:
        self._values = values
        self._subscribers: list[Subscription] = []

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, modes: Iterable[str], *, run_id: str | None = None) -> Subscription:
        """Start receiving at once, before the first iteration; `run_id` None means every run."""
        wanted = frozenset(modes)
        unknown = wanted - STREAM_MODES
        if not wanted or unknown:
            raise RuntimeControlError(f"stream modes must be among {sorted(STREAM_MODES)}, got {sorted(wanted)}")
        if "values" in wanted and run_id is None:
            raise RuntimeControlError("the values mode is a snapshot of one run; give a run_id")
        subscription = Subscription(self, wanted, run_id)
        self._subscribers.append(subscription)
        return subscription

    async def snapshot(self, run_id: str) -> Event | None:
        if self._values is None:
            return None
        return Event(type=VALUES_EVENT, run_id=run_id, payload=await self._values(run_id))

    async def on_event(self, event: Event) -> None:
        values_wanted = False
        for subscription in list(self._subscribers):
            if subscription.run_id is not None and subscription.run_id != event.run_id:
                continue
            if "debug" in subscription.modes or any(
                event.type in _TYPES.get(mode, ()) for mode in subscription.modes
            ):
                subscription.put(event)
            if "values" in subscription.modes and event.type in _CHANGES_VALUES:
                values_wanted = True
        if values_wanted and event.run_id is not None:
            snapshot = await self.snapshot(event.run_id)
            if snapshot is not None:
                for subscription in list(self._subscribers):
                    if "values" in subscription.modes and subscription.run_id == event.run_id:
                        subscription.put(snapshot)

    async def close(self) -> None:
        for subscription in list(self._subscribers):
            subscription.put(_END)

    def _drop(self, subscription: Subscription) -> None:
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)
