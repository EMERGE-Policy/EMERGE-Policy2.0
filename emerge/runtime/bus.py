"""The message hub in the main process, and the pipe each node process talks to it through.

Every node process has one duplex pipe to the bus, read and written by background threads on both
ends so neither event loop ever blocks on it. Everything crossing it is a JSON-ready dict with a
"kind" (see `Kind`). The bus is the only place that stamps: it gives each published message its
version, puts every stamped item on the run's one timeline, writes what a reopened session needs to
Storage, and only then passes it on.

Incoming items are handled one at a time, in arrival order, and every stamp is taken under one lock,
so timeline numbers increase in the order things reached the bus. Anything from an instance that is
no longer the current one for its node is dropped, leaving only a `node.stale` trace record.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from multiprocessing.connection import Connection
from typing import TYPE_CHECKING, Any, Protocol

from emerge.callbacks import EventEmitter
from emerge.events import Event, EventType
from emerge.exceptions import RuntimeControlError
from emerge.graph.graph import Graph
from emerge.graph.ports import MainAgentMessage, Message, Ref
from emerge.runtime.state import LifecycleState, NodeRuntimeState
from emerge.storage.base import AskRecord, Storage

if TYPE_CHECKING:
    from emerge.runtime.process import NodeProcess

logger = logging.getLogger(__name__)

# Trace-only record of something an instance sent after it stopped being the current one.
NODE_STALE = "node.stale"

# Event types a node may push with `emit_event`: its own custom events, and the process events of
# the agent modules it runs (model client, agent loop, tools). Everything else is the runtime's.
NODE_EVENT_TYPES = frozenset(
    {
        EventType.NODE_CUSTOM,
        EventType.MODEL_STARTED,
        EventType.MODEL_TOKEN,
        EventType.MODEL_FINISHED,
        EventType.MODEL_USAGE,
        EventType.TOOL_STARTED,
        EventType.TOOL_FINISHED,
        EventType.AGENT_MESSAGE,
    }
)


class Kind(StrEnum):
    """The "kind" of a dict on a node pipe."""

    # node -> bus
    READY = "ready"  # setup() returned; run() is next
    PUBLISH = "publish"  # {req, port, text, data, refs}
    REPORT = "report"  # {text}
    EVENT = "event"  # {event: Event.to_dict()}
    ASK = "ask"  # {req, question}; the reply carries {answer}
    SNAPSHOT = "snapshot"  # {data}
    FETCH = "fetch"  # {req, port}; the reply carries {source, messages}, read from Storage
    LOG = "log"  # {level, name, message}
    EXIT = "exit"  # {result, outcome, reason, error, returned}; the last thing sent
    # bus -> node
    CONTROL = "control"  # {op: pause | resume | stop}
    PORT_MESSAGE = "port_message"  # {port, message}: a message for one of the node's inputs
    INPUT = "input"  # {port, source, closed, start}: an input was rewired or closed
    REPLY = "reply"  # {req, ok, result, error, stop}
    MAIN_AGENT_MESSAGE = "main_agent_message"  # {message}
    # posted by the reading thread itself once the other end has closed the pipe
    DISCONNECTED = "disconnected"


def reply(req: int, result: Any = None, *, error: str | None = None, stop: bool = False) -> dict[str, Any]:
    # `stop` tells the node to raise StopRequested instead of NodeError: the instance was replaced.
    return {"kind": Kind.REPLY, "req": req, "ok": error is None, "result": result, "error": error, "stop": stop}


def input_message(port: str, source: str | None, closed: bool, start: int | None = None) -> dict[str, Any]:
    # `start`: the input reads what its source publishes after this version. Set on a rewiring to the
    # new source's latest version at that moment; None reads from the oldest message still kept.
    return {"kind": Kind.INPUT, "port": port, "source": source, "closed": closed, "start": start}


class Channel:
    """One end of a node pipe: a thread reads, a thread writes, the event loop does neither."""

    def __init__(
        self,
        conn: Connection,
        loop: asyncio.AbstractEventLoop,
        on_message: Callable[[dict[str, Any]], None],
        *,
        name: str = "",
    ) -> None:
        self._conn = conn
        self._loop = loop
        self._on_message = on_message
        self._outbox: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
        self._reader = threading.Thread(target=self._read, name=f"pipe-reader:{name}", daemon=True)
        self._writer = threading.Thread(target=self._write, name=f"pipe-writer:{name}", daemon=True)

    def start(self) -> None:
        self._reader.start()
        self._writer.start()

    def send(self, message: dict[str, Any]) -> None:
        """Queue a message; it goes out in call order. Never blocks."""
        self._outbox.put(message)

    def close(self) -> None:
        """Send everything queued, then close the pipe. Blocks, so call it from a thread."""
        self._outbox.put(None)
        self._writer.join()
        self._conn.close()

    def _read(self) -> None:
        while True:
            try:
                message = self._conn.recv()
            except (EOFError, OSError):
                self._post({"kind": Kind.DISCONNECTED})
                return
            self._post(message)

    def _post(self, message: dict[str, Any]) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_message, message)
        except RuntimeError:  # the loop is closed: nobody is listening any more
            pass

    def _write(self) -> None:
        while (message := self._outbox.get()) is not None:
            try:
                self._conn.send(message)
            except (OSError, ValueError):  # the other end is gone; what is left has no reader
                return


class LifecycleHandler(Protocol):
    """What the bus hands to the supervisor: a process got going, said how it ended, or went away."""

    async def on_node_ready(self, process: NodeProcess) -> None: ...

    async def on_node_exit(self, process: NodeProcess, report: dict[str, Any]) -> None: ...

    async def on_node_disconnected(self, process: NodeProcess) -> None: ...


class MessageBus:
    """Routes, stamps and stores everything between node processes and the main process."""

    def __init__(self, run_id: str, storage: Storage, callbacks: EventEmitter, *, port_buffer: int) -> None:
        self.run_id = run_id
        self.storage = storage
        self.callbacks = callbacks
        self.port_buffer = port_buffer
        # Kept current by the supervisor; routing and the current-instance check read it.
        self.graph = Graph()
        self.lifecycle: LifecycleHandler | None = None
        self._processes: dict[str, NodeProcess] = {}
        self._inbox: asyncio.Queue[tuple[NodeProcess, dict[str, Any]] | None] = asyncio.Queue()
        self._timeline = asyncio.Lock()
        # Last version per logical port (node, port). A replacement keeps counting, so a subscriber's
        # cursor stays valid across it.
        self._versions: dict[tuple[str, str], int] = {}
        # Last MainAgentMessage.seq per instance.
        self._main_seq: dict[str, int] = {}
        self._asks: dict[str, tuple[NodeProcess, int]] = {}
        self._dispatcher: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # Versions continue from what is stored, so a run carried on after its session was reopened
        # never hands out a version twice.
        for record in await self.storage.latest_port_messages(self.run_id):
            self._versions[(record.node, record.port)] = record.version
        # Likewise the numbers of messages queued for instances that had not started: an instance
        # still waiting gets its next one after them.
        for queued in await self.storage.queued_node_messages(self.run_id, include_delivered=True):
            self._main_seq[queued.instance_id] = max(self._main_seq.get(queued.instance_id, 0), queued.seq)
        self._dispatcher = asyncio.create_task(self._dispatch(), name=f"bus:{self.run_id}")

    async def close(self) -> None:
        """Handle what has already arrived, then stop."""
        if self._dispatcher is not None:
            await self._inbox.put(None)
            await self._dispatcher
            self._dispatcher = None

    # -- processes ----------------------------------------------------------

    def post(self, process: NodeProcess, message: dict[str, Any]) -> None:
        """Entry point for a process's pipe reader; runs on the event loop thread."""
        self._inbox.put_nowait((process, message))

    async def attach(self, process: NodeProcess) -> None:
        self._processes[process.instance_id] = process
        # Main-agent messages sent while the instance was still pending go first, in seq order.
        queued = await self.storage.queued_node_messages(self.run_id, instance_id=process.instance_id)
        for item in queued:
            process.send({"kind": Kind.MAIN_AGENT_MESSAGE, "message": item.message.model_dump(mode="json")})
        if queued:
            await self.storage.mark_node_messages_delivered(self.run_id, process.instance_id)

    def detach(self, process: NodeProcess) -> None:
        if self._processes.get(process.instance_id) is process:
            del self._processes[process.instance_id]

    def is_current(self, process: NodeProcess) -> bool:
        return self.graph.is_current(process.node_id, process.instance_id)

    def current_process(self, node_id: str) -> NodeProcess | None:
        spec = self.graph.node(node_id)
        return None if spec is None else self._processes.get(spec.instance_id)

    def latest_version(self, node_id: str, port: str) -> int:
        """The version most recently given to a message on output `node_id.port`, 0 before any."""
        return self._versions.get((node_id, port), 0)

    def has_ask(self, ask_id: str) -> bool:
        return ask_id in self._asks

    # -- stamped writes from the main process --------------------------------

    async def record_state(
        self,
        record: NodeRuntimeState,
        *,
        previous: LifecycleState | None,
        irreversible: bool,
        current: bool,
    ) -> None:
        """Store an instance's new state; for the current instance, also stamp it and announce it.

        A replaced instance's own record still changes as it ends, but nobody downstream hears of
        it: it is written to Storage under its own instance id and traced as stale.
        """
        if not current:
            await self._store_state(record)
            self.emit_stale(record.node_id, record.instance_id, irreversible, "state", state=str(record.state))
            return
        async with self._timeline:
            content_time = await self.storage.next_content_time(self.run_id)
            await self._store_state(record)
            self._emit(
                EventType.NODE_STATE,
                record.node_id,
                record.instance_id,
                irreversible,
                content_time,
                {
                    "state": str(record.state),
                    "previous": None if previous is None else str(previous),
                    "outcome": record.outcome,
                    "reason": record.reason,
                    "error": record.error,
                    "pid": record.pid,
                },
            )

    async def deliver_main_agent_message(
        self,
        node_id: str,
        instance_id: str,
        irreversible: bool,
        *,
        text: str | None = None,
        data: Mapping[str, Any] | None = None,
        refs: Iterable[Ref] = (),
    ) -> MainAgentMessage:
        """Stamp a message from the main agent and hand it to one instance.

        An instance that has not started gets it when it starts: until then the message waits in
        Storage, so it survives the session being closed and reopened.
        """
        async with self._timeline:
            content_time = await self.storage.next_content_time(self.run_id)
            seq = self._main_seq.get(instance_id, 0) + 1
            message = MainAgentMessage(
                seq=seq, timestamp=time.time(), content_time=content_time, text=text or "", data=data, refs=tuple(refs)
            )
            self._main_seq[instance_id] = seq
            payload = message.model_dump(mode="json")
            self._emit(EventType.NODE_MAIN_AGENT_MESSAGE, node_id, instance_id, irreversible, content_time, payload)
            wire = {"kind": Kind.MAIN_AGENT_MESSAGE, "message": payload}
            process = self._processes.get(instance_id)
            if process is None:
                await self.storage.queue_node_message(self.run_id, node_id, instance_id, message)
            else:
                process.send(wire)
        return message

    async def answer_ask(self, ask_id: str, answer: str) -> AskRecord:
        """Store the main agent's answer, stamp it, and send it back to the instance that asked."""
        async with self._timeline:
            waiting = self._asks.pop(ask_id, None)
            if waiting is None:
                raise RuntimeControlError(f"ask {ask_id!r} is not waiting for an answer")
            process, req = waiting
            content_time = await self.storage.next_content_time(self.run_id)
            record = await self.storage.answer_ask(ask_id, answer)
            self._emit(
                EventType.NODE_ANSWER,
                process.node_id,
                process.instance_id,
                process.irreversible,
                content_time,
                {"ask_id": ask_id, "answer": answer},
            )
            process.send(reply(req, {"answer": answer}))
        return record

    async def void_asks(self, instance_id: str) -> None:
        """Questions from an instance that ended will never be answered."""
        for ask_id, (process, _) in list(self._asks.items()):
            if process.instance_id == instance_id:
                del self._asks[ask_id]
                await self.storage.void_ask(ask_id)

    def emit_stale(self, node_id: str, instance_id: str, irreversible: bool, kind: str, **detail: Any) -> None:
        self.callbacks.emit_nowait(
            Event(
                type=NODE_STALE,
                run_id=self.run_id,
                node_id=node_id,
                instance_id=instance_id,
                irreversible=irreversible,
                payload={"kind": str(kind), **detail},
            )
        )

    # -- what node processes send ---------------------------------------------

    async def _dispatch(self) -> None:
        while (item := await self._inbox.get()) is not None:
            process, message = item
            try:
                await self._handle(process, message)
            except Exception:
                logger.exception("bus could not handle %r from %s", message.get("kind"), process.instance_id)

    async def _handle(self, process: NodeProcess, message: dict[str, Any]) -> None:
        kind = message["kind"]
        if kind == Kind.LOG:
            logging.getLogger(f"emerge.node.{process.node_id}").log(
                message["level"], "[%s] %s", process.instance_id, message["message"]
            )
            return
        assert self.lifecycle is not None
        # Lifecycle items are handled for replaced instances too: the old instance's own record
        # still has to end. The supervisor keeps that from reaching anything downstream.
        if kind == Kind.READY:
            await self.lifecycle.on_node_ready(process)
            return
        if kind == Kind.EXIT:
            await self.lifecycle.on_node_exit(process, message)
            return
        if kind == Kind.DISCONNECTED:
            await self.lifecycle.on_node_disconnected(process)
            return
        if not self.is_current(process):
            self.emit_stale(process.node_id, process.instance_id, process.irreversible, kind, port=message.get("port"))
            if "req" in message:
                process.send(reply(message["req"], error="this instance has been replaced and is stopping", stop=True))
            return
        handler = self._handlers[kind]
        try:
            await handler(self, process, message)
        except Exception as e:
            if "req" not in message:
                raise
            process.send(reply(message["req"], error=f"{type(e).__name__}: {e}"))

    async def _publish(self, process: NodeProcess, message: dict[str, Any]) -> None:
        port = message["port"]
        key = (process.node_id, port)
        async with self._timeline:
            content_time = await self.storage.next_content_time(self.run_id)
            version = self._versions.get(key, 0) + 1
            published = Message(
                source_node=process.node_id,
                source_instance=process.instance_id,
                source_port=port,
                version=version,
                timestamp=time.time(),
                content_time=content_time,
                text=message["text"],
                data=message["data"],
                refs=tuple(Ref.model_validate(ref) for ref in message["refs"]),
            )
            await self.storage.put_port_message(published, run_id=self.run_id)
            self._versions[key] = version
            payload = published.model_dump(mode="json")
            process.send(reply(message["req"], payload))
            for sub in self.graph.consumers_of(process.node_id, port):
                target = self.current_process(sub.target_node)
                if target is not None:
                    target.send({"kind": Kind.PORT_MESSAGE, "port": sub.target_port, "message": payload})
            self._emit(
                EventType.PORT_PUBLISHED,
                process.node_id,
                process.instance_id,
                process.irreversible,
                content_time,
                {"port": port, **{key: payload[key] for key in ("version", "text", "data", "refs")}},
            )

    async def _report(self, process: NodeProcess, message: dict[str, Any]) -> None:
        async with self._timeline:
            content_time = await self.storage.next_content_time(self.run_id)
            self._emit(
                EventType.NODE_REPORT,
                process.node_id,
                process.instance_id,
                process.irreversible,
                content_time,
                {"text": message["text"]},
            )

    async def _event(self, process: NodeProcess, message: dict[str, Any]) -> None:
        # The node supplies type and payload only; who sent it and when it happened are the bus's.
        event = dataclasses.replace(
            Event.from_dict(message["event"]),
            run_id=self.run_id,
            node_id=process.node_id,
            instance_id=process.instance_id,
            irreversible=process.irreversible,
            content_time=None,
        )
        if event.type != EventType.NODE_CUSTOM:
            # Model tokens, tool calls and the like are process events and are not stamped.
            self.callbacks.emit_nowait(event)
            return
        async with self._timeline:
            content_time = await self.storage.next_content_time(self.run_id)
            self.callbacks.emit_nowait(dataclasses.replace(event, content_time=content_time))

    async def _ask(self, process: NodeProcess, message: dict[str, Any]) -> None:
        async with self._timeline:
            content_time = await self.storage.next_content_time(self.run_id)
            record = await self.storage.add_ask(self.run_id, process.node_id, process.instance_id, message["question"])
            self._asks[record.id] = (process, message["req"])
            self._emit(
                EventType.NODE_ASK,
                process.node_id,
                process.instance_id,
                process.irreversible,
                content_time,
                {"ask_id": record.id, "question": record.question},
            )

    async def _snapshot(self, process: NodeProcess, message: dict[str, Any]) -> None:
        # Overwritten in place and pushed to nobody: the main agent reads it when it looks.
        await self.storage.set_node_snapshot(self.run_id, process.node_id, process.instance_id, message["data"])

    async def _fetch(self, process: NodeProcess, message: dict[str, Any]) -> None:
        # What an input catches up from: everything its source port still keeps, as stored. The
        # reply names the source it read, so the node can drop it if the input was rewired meanwhile.
        sub = self.graph.source_of(process.node_id, message["port"])
        if sub is None:
            process.send(reply(message["req"], {"source": None, "messages": []}))
            return
        records = await self.storage.port_messages(self.run_id, sub.source_node, sub.source_port)
        process.send(
            reply(
                message["req"],
                {
                    "source": f"{sub.source_node}.{sub.source_port}",
                    "messages": [record.message.model_dump(mode="json") for record in records],
                },
            )
        )

    _handlers: dict[str, Callable[[MessageBus, NodeProcess, dict[str, Any]], Any]] = {
        Kind.PUBLISH: _publish,
        Kind.REPORT: _report,
        Kind.EVENT: _event,
        Kind.ASK: _ask,
        Kind.SNAPSHOT: _snapshot,
        Kind.FETCH: _fetch,
    }

    # -- helpers --------------------------------------------------------------

    async def _store_state(self, record: NodeRuntimeState) -> None:
        await self.storage.set_node_state(
            self.run_id,
            record.node_id,
            record.instance_id,
            str(record.state),  # type: ignore[arg-type]
            outcome=record.outcome,
            reason=record.reason,
            error=record.error,
        )

    def _emit(
        self,
        event_type: str,
        node_id: str,
        instance_id: str,
        irreversible: bool,
        content_time: int | None,
        payload: dict[str, Any],
    ) -> None:
        self.callbacks.emit_nowait(
            Event(
                type=event_type,
                run_id=self.run_id,
                node_id=node_id,
                instance_id=instance_id,
                content_time=content_time,
                irreversible=irreversible,
                payload=payload,
            )
        )
