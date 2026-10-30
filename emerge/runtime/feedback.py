"""What the nodes and the run tell the main agent, and the run summary it reads each turn.

`FeedbackHandler` is a callback handler: it watches the run's events and hands the main agent
`Feedback` items through an injected callback, or a queue when there is none, without importing
the agent. It decides how often, not what: a node's reports say what the node's author wanted.

- Pushed at once: a node ended (its outcome and reason, or its state and error), a node that can
  never start, the graph went idle, a node asked a question.
- A node's reports and its other state changes (starting, running, paused, stopping) are merged
  per node and kind and pushed as one batch per time window.
- Not pushed: new port messages and custom events, which are for the interface and outside
  subscribers.

`summary()` is the short view the main agent's context gets: each node's state, the latest
notable events, the questions waiting for an answer; cut to a length limit.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from emerge.callbacks import CallbackHandler
from emerge.events import Event, EventType
from emerge.runtime.bus import NODE_STALE
from emerge.runtime.run import RUN_IDLE, RUN_STATUS

FeedbackKind = Literal["node_ended", "node_skipped", "graph_idle", "ask", "batch"]

_ENDED = frozenset({"finished", "failed", "stopped"})


class Feedback(BaseModel):
    """One item for the main agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    kind: FeedbackKind
    # The item as one or a few lines of text, ready for the agent's context.
    text: str
    node_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)


@dataclass
class _NodeView:
    state: str = "pending"
    instance_id: str | None = None
    outcome: str | None = None
    reason: str = ""
    error: str | None = None


@dataclass
class _Merged:
    """Events of one kind from one node within the current window."""

    count: int = 0
    last: str = ""


@dataclass
class _RunView:
    run_id: str
    status: str = "running"
    nodes: dict[str, _NodeView] = field(default_factory=dict)
    # ask id -> (node id, instance id, question)
    asks: dict[str, tuple[str, str | None, str]] = field(default_factory=dict)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    batch: dict[tuple[str, str], _Merged] = field(default_factory=dict)
    flush: asyncio.Task[None] | None = None


class FeedbackHandler(CallbackHandler):
    """Routes run events to the main agent, at once or merged per time window."""

    def __init__(
        self,
        deliver: Callable[[Feedback], Awaitable[Any] | Any] | None = None,
        *,
        interval: float = 1.0,
        recent: int = 20,
        max_chars: int = 4000,
    ) -> None:
        self._deliver = deliver
        self.interval = interval
        self.max_chars = max_chars
        self._recent = recent
        # Where items go when no callback was given.
        self.queue: asyncio.Queue[Feedback] = asyncio.Queue()
        self._runs: dict[str, _RunView] = {}
        # Pushes leave in the order they were made, batches and immediate items alike.
        self._push_lock = asyncio.Lock()

    # -- set up -------------------------------------------------------------

    def seed(
        self,
        run_id: str,
        *,
        status: str,
        nodes: Mapping[str, Mapping[str, Any]],
        asks: Sequence[tuple[str, str, str | None, str]] = (),
    ) -> None:
        """What the run looks like before any event, e.g. a run carried on after its session reopened.

        `nodes`: node id -> {state, instance_id, outcome, reason, error}; `asks`: (ask id, node id,
        instance id, question) still waiting.
        """
        view = self._view(run_id)
        view.status = status
        for node_id, fields in nodes.items():
            view.nodes[node_id] = _NodeView(
                state=str(fields.get("state", "pending")),
                instance_id=fields.get("instance_id"),
                outcome=fields.get("outcome"),
                reason=str(fields.get("reason") or ""),
                error=fields.get("error"),
            )
        for ask_id, node_id, instance_id, question in asks:
            view.asks[ask_id] = (node_id, instance_id, question)

    def forget(self, run_id: str) -> None:
        view = self._runs.pop(run_id, None)
        if view is not None and view.flush is not None:
            view.flush.cancel()

    # -- events -------------------------------------------------------------

    async def on_event(self, event: Event) -> None:
        if event.run_id is None:
            return
        view = self._view(event.run_id)
        kind = event.type
        node_id = event.node_id
        payload = event.payload
        if kind == EventType.NODE_STATE and node_id is not None:
            await self._on_state(view, event)
        elif kind == NODE_STALE and payload.get("kind") == "state" and payload.get("state") in _ENDED | {"skipped"}:
            # A replaced instance ended: its questions will never be answered.
            self._drop_asks(view, event.instance_id)
        elif kind == EventType.NODE_ASK and node_id is not None:
            question = str(payload.get("question", ""))
            view.asks[payload["ask_id"]] = (node_id, event.instance_id, question)
            view.recent.append(f"{node_id} asked ({payload['ask_id']}): {question}")
            await self._push(
                view,
                Feedback(run_id=event.run_id, kind="ask", node_id=node_id, text=f"{node_id} asks ({payload['ask_id']}): {question}", data=dict(payload)),
            )
        elif kind == EventType.NODE_ANSWER:
            view.asks.pop(payload.get("ask_id"), None)
        elif kind == RUN_IDLE:
            view.recent.append("graph idle: every node has ended")
            await self._push(
                view,
                Feedback(
                    run_id=event.run_id,
                    kind="graph_idle",
                    text=f"the graph is idle: every node has ended (graph version {payload.get('graph_version')}); "
                    "add nodes to go on or end the run",
                    data=dict(payload),
                ),
            )
        elif kind == RUN_STATUS:
            view.status = str(payload.get("status"))
        elif kind == EventType.GRAPH_PATCHED:
            view.recent.append(f"graph changed: version {payload.get('previous_version')} -> {payload.get('version')}")
        elif node_id is not None and kind == EventType.NODE_REPORT:
            self._merge(view, node_id, "report", str(payload.get("text", "")))

    async def _on_state(self, view: _RunView, event: Event) -> None:
        assert event.node_id is not None and event.run_id is not None
        node_id, payload = event.node_id, event.payload
        state = str(payload.get("state"))
        node = view.nodes.setdefault(node_id, _NodeView())
        node.state, node.instance_id = state, event.instance_id
        node.outcome, node.reason, node.error = payload.get("outcome"), str(payload.get("reason") or ""), payload.get("error")
        if state in _ENDED:
            self._drop_asks(view, event.instance_id)
            text = _ended_text(node_id, node, bool(event.irreversible))
            view.recent.append(text)
            data = {key: payload.get(key) for key in ("state", "outcome", "reason", "error")}
            await self._push(view, Feedback(run_id=event.run_id, kind="node_ended", node_id=node_id, text=text, data=data))
        elif state == "skipped":
            text = f"{node_id} can never start: none of its start edges can fire any more (skipped)"
            view.recent.append(text)
            await self._push(view, Feedback(run_id=event.run_id, kind="node_skipped", node_id=node_id, text=text))
        else:
            self._merge(view, node_id, "state", state)

    # -- summary ------------------------------------------------------------

    def summary(self, run_id: str, *, max_chars: int | None = None) -> str:
        """Node states, the latest notable events and the open questions, within `max_chars`."""
        limit = self.max_chars if max_chars is None else max_chars
        view = self._runs.get(run_id) or _RunView(run_id)
        lines = [f"run {run_id}: {view.status}"]
        if view.asks:
            lines.append("questions waiting for an answer:")
            lines += [f"  {ask_id} from {node_id}: {question}" for ask_id, (node_id, _, question) in view.asks.items()]
        live = [(node_id, node) for node_id, node in view.nodes.items() if node.state not in _ENDED | {"skipped"}]
        ended = [(node_id, node) for node_id, node in view.nodes.items() if node.state in _ENDED | {"skipped"}]
        if view.nodes:
            lines.append("nodes:")
        lines += [f"  {node_id}: {node.state}" for node_id, node in live]
        lines += [f"  {_ended_text(node_id, node, False)}" for node_id, node in ended]
        if view.recent:
            lines.append("recent:")
            lines += [f"  {line}" for line in list(view.recent)[-self._recent :]]
        return _fit(lines, limit)

    # -- plumbing -----------------------------------------------------------

    async def close(self) -> None:
        for view in list(self._runs.values()):
            if view.flush is not None:
                view.flush.cancel()
                view.flush = None
            await self._flush(view)

    def _view(self, run_id: str) -> _RunView:
        view = self._runs.get(run_id)
        if view is None:
            view = self._runs[run_id] = _RunView(run_id, recent=deque(maxlen=self._recent))
        return view

    def _drop_asks(self, view: _RunView, instance_id: str | None) -> None:
        for ask_id in [ask_id for ask_id, (_, instance, _) in view.asks.items() if instance == instance_id]:
            del view.asks[ask_id]

    def _merge(self, view: _RunView, node_id: str, kind: str, text: str) -> None:
        merged = view.batch.setdefault((node_id, kind), _Merged())
        merged.count += 1
        merged.last = text
        if view.flush is None:
            view.flush = asyncio.create_task(self._flush_later(view), name=f"feedback:{view.run_id}")

    async def _flush_later(self, view: _RunView) -> None:
        await asyncio.sleep(self.interval)
        view.flush = None
        await self._flush(view)

    async def _flush(self, view: _RunView) -> None:
        if not view.batch:
            return
        batch, view.batch = view.batch, {}
        lines = []
        for (node_id, kind), merged in batch.items():
            times = f"{merged.count} x " if merged.count > 1 else ""
            lines.append(f"{node_id}: {times}{kind}, latest: {merged.last}")
        data = {f"{node_id}/{kind}": {"count": m.count, "latest": m.last} for (node_id, kind), m in batch.items()}
        await self._send(Feedback(run_id=view.run_id, kind="batch", text="\n".join(lines), data=data))

    async def _push(self, view: _RunView, item: Feedback) -> None:
        # What was merged so far happened before this, so it goes first.
        if view.flush is not None:
            view.flush.cancel()
            view.flush = None
        await self._flush(view)
        await self._send(item)

    async def _send(self, item: Feedback) -> None:
        async with self._push_lock:
            if self._deliver is None:
                self.queue.put_nowait(item)
                return
            result = self._deliver(item)
            if inspect.isawaitable(result):
                await result


def _ended_text(node_id: str, node: _NodeView, irreversible: bool) -> str:
    mark = " [irreversible]" if irreversible else ""
    if node.state == "finished":
        reason = f" ({node.reason})" if node.reason else ""
        return f"{node_id}{mark} finished: {node.outcome}{reason}"
    if node.state == "skipped":
        return f"{node_id}: skipped"
    error = f": {node.error}" if node.error else ""
    return f"{node_id}{mark} {node.state}{error}"


def _fit(lines: list[str], limit: int) -> str:
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        tail = f"... {len(lines) - index} more line(s) left out"
        if used + len(line) + 1 + len(tail) > limit:
            kept.append(tail)
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)[:limit]
