"""`NodeContext` inside a node process: every call becomes a message to or from the bus.

The context keeps, per input, the messages its source port still holds: filled from Storage the
first time the input is read, then by the bus's pushes, and cut to the same limit as the port. All
waiting happens on this process's event loop; one signal wakes every waiter whenever anything
arrives, and each waiter re-checks its own condition. A stop request wins over everything that is
waiting: it raises StopRequested at the next check.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from emerge.config import WorkspaceConfig
from emerge.events import Event, EventType
from emerge.exceptions import InputClosed, NodeError, StopRequested, WaitTimeout
from emerge.graph.node import Node
from emerge.graph.ports import MainAgentMessage, Message, Ref, thaw
from emerge.runtime.bus import NODE_EVENT_TYPES, Kind
from emerge.storage.artifacts import ensure_node_dir, ref_dir

if TYPE_CHECKING:
    from emerge.models.client import ModelClient
    from emerge.runtime.process import NodeLaunch

Send = Callable[[dict[str, Any]], None]

# What `iter_input_messages` waits for besides a message: the input closed and nothing is left.
_CLOSED = object()


class _Signal:
    """Wakes every current waiter at once; a waiter then re-checks its own condition."""

    def __init__(self) -> None:
        self._waiters: set[asyncio.Future[None]] = set()

    def notify(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    async def wait(self, timeout: float | None) -> None:
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await asyncio.wait((waiter,), timeout=timeout)
        finally:
            self._waiters.discard(waiter)


class _Input:
    """One input port as this process sees it."""

    def __init__(self, source: str | None, closed: bool, start: int | None = None) -> None:
        # "node.port" of the output it subscribes to, None while unconnected.
        self.source = source
        self.closed = closed
        # Versions up to this one are not handed out by waits and iteration, only by
        # get_latest_input_message: they were published before the input was wired to this source.
        self.start = start
        self.messages: dict[int, Message] = {}
        # Whether what the source port keeps in Storage has been read in since the last (re)wiring.
        self.synced = source is None
        # Bumped on every rewiring, so a wait that started before can tell and start over.
        self.generation = 0
        # Last version handed out by iter_input_messages; None means "from the oldest kept".
        self.cursor: int | None = None

    def add(self, message: Message, limit: int) -> None:
        if f"{message.source_node}.{message.source_port}" != self.source:
            return
        self.messages[message.version] = message
        if len(self.messages) > limit:
            for version in sorted(self.messages)[:-limit]:
                del self.messages[version]

    def update(self, source: str | None, closed: bool, start: int | None) -> None:
        if source != self.source:
            # Rewired: what was read belongs to the old source. Reading goes on with what the new
            # source publishes from the moment of the rewiring, after version `start`.
            self.source = source
            self.start = start
            self.messages.clear()
            self.synced = source is None
            self.cursor = None
            self.generation += 1
        self.closed = closed

    def after(self, version: int | None) -> list[int]:
        floor = version if self.start is None else self.start if version is None else max(version, self.start)
        return sorted(v for v in self.messages if floor is None or v > floor)

    def latest(self) -> Message | None:
        return self.messages[max(self.messages)] if self.messages else None


class _BusEmitter:
    """EventEmitter for code running inside the node, e.g. its model client or agent loop."""

    def __init__(self, send: Send) -> None:
        self._send = send

    async def emit(self, event: Event) -> None:
        self.emit_nowait(event)

    def emit_nowait(self, event: Event) -> None:
        self._send({"kind": Kind.EVENT, "event": event.to_dict()})

    def with_event_defaults(self, **_: Any) -> _BusEmitter:
        # The bus fills run, node, instance and irreversible itself; there is nothing to default.
        return self


class RuntimeNodeContext:
    """The NodeContext a node's setup / run / teardown receive."""

    def __init__(self, launch: NodeLaunch, send: Send) -> None:
        self.node_id = launch.node_id
        self.instance_id = launch.instance_id
        self.run_id = launch.run_id
        self.workspace = Path(launch.workspace)
        config = WorkspaceConfig.model_validate(launch.config)
        config._workspace = self.workspace
        self.config = config
        self.artifacts_dir = ensure_node_dir(self.workspace, self.run_id, self.node_id, self.instance_id)
        self.instance_description = launch.description
        self.outcomes: tuple[str, ...] = ()
        self.emitter = _BusEmitter(send)
        self._type = launch.node_type
        self._outputs: frozenset[str] = frozenset()
        self._send = send
        self._port_buffer = launch.port_buffer
        self._inputs = {
            port: _Input(state["source"], state["closed"], state.get("start")) for port, state in launch.inputs.items()
        }
        self._main_agent_messages: deque[MainAgentMessage] = deque()
        self._paused = launch.paused
        self._stop_requested = False
        self._signal = _Signal()
        self._requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._last_request = 0
        self._model_client: ModelClient | None = None

    def bind(self, cls: type[Node]) -> None:
        """Take the declarations of the node class once it is loaded."""
        self.outcomes = cls.outcomes
        self._outputs = frozenset(port.name for port in cls.outputs)

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    # -- from the bus -------------------------------------------------------

    def receive(self, message: dict[str, Any]) -> None:
        """Called on this process's event loop for every dict the bus sends."""
        kind = message["kind"]
        if kind == Kind.REPLY:
            future = self._requests.pop(message["req"], None)
            if future is not None and not future.done():
                future.set_result(message)
        elif kind == Kind.PORT_MESSAGE:
            self._inputs[message["port"]].add(Message.model_validate(message["message"]), self._port_buffer)
        elif kind == Kind.INPUT:
            self._inputs[message["port"]].update(message["source"], message["closed"], message.get("start"))
        elif kind == Kind.MAIN_AGENT_MESSAGE:
            self._main_agent_messages.append(MainAgentMessage.model_validate(message["message"]))
        elif kind == Kind.CONTROL:
            op = message["op"]
            if op == "pause":
                self._paused = True
            elif op == "resume":
                self._paused = False
            elif op == "stop":
                self._stop_requested = True
        self._signal.notify()

    # -- inputs -------------------------------------------------------------

    async def get_latest_input_message(self, port: str) -> Message | None:
        return (await self._synced(port)).latest()

    async def wait_for_input_message(
        self,
        port: str,
        newer_than: int | None = None,
        timeout: float | None = None,
        where: Callable[[Message], bool] | None = None,
    ) -> Message:
        inp = self._input(port)
        generation, after, checked = inp.generation, newer_than, None

        def check() -> Message | None:
            nonlocal generation, after, checked
            if inp.generation != generation:
                generation, after, checked = inp.generation, None, None
            for version in inp.after(after):
                if where is not None:
                    # `where` sees each message once, however often this wakes up.
                    if checked is not None and version <= checked:
                        continue
                    checked = version
                    if not where(inp.messages[version]):
                        continue
                return inp.messages[version]
            if inp.closed:
                raise InputClosed(f"input {port!r} is closed and holds no {'matching ' if where else ''}message after version {after}")
            return None

        return await self._wait(check, timeout, f"a message on input {port!r}", ports=(port,))

    async def wait_for_input_messages(
        self,
        ports: Sequence[str],
        *,
        newer_than: Mapping[str, int] | None = None,
        match: Callable[[Message], Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Message]:
        ports = tuple(ports)
        if not ports or len(set(ports)) != len(ports):
            raise NodeError(f"wait_for_input_messages needs distinct input ports, got {ports!r}")
        inputs = {port: self._input(port) for port in ports}
        cursors: dict[str, int | None] = {port: (newer_than or {}).get(port) for port in ports}
        generations = {port: inputs[port].generation for port in ports}
        # match() results by (port, version), so each message is looked at once.
        keys: dict[tuple[str, int], Any] = {}

        def check() -> dict[str, Message] | None:
            for port in ports:
                if inputs[port].generation != generations[port]:
                    generations[port], cursors[port] = inputs[port].generation, None
                    for key in [key for key in keys if key[0] == port]:
                        del keys[key]
            candidates = {port: inputs[port].after(cursors[port]) for port in ports}
            if match is None:
                if all(candidates.values()):
                    return {port: inputs[port].messages[candidates[port][0]] for port in ports}
                closed = [port for port in ports if not candidates[port] and inputs[port].closed]
                if closed:
                    raise InputClosed(f"input {closed[0]!r} is closed and holds no message after version {cursors[closed[0]]}")
                return None
            # Per port, the earliest message carrying each value; the values must be hashable.
            earliest: dict[str, dict[Any, Message]] = {}
            for port in ports:
                by_value: dict[Any, Message] = {}
                for version in candidates[port]:
                    if (port, version) not in keys:
                        keys[(port, version)] = match(inputs[port].messages[version])
                    by_value.setdefault(keys[(port, version)], inputs[port].messages[version])
                earliest[port] = by_value
            common = set.intersection(*(set(values) for values in earliest.values()))
            if common:
                # The group that was complete first: its last message reached the bus earliest.
                value = min(common, key=lambda v: max(earliest[port][v].content_time for port in ports))
                return {port: earliest[port][value] for port in ports}
            closed = [port for port in ports if inputs[port].closed]
            # A closed input's messages are final, so only values they all hold can still pair up.
            if closed and not set.intersection(*(set(earliest[port]) for port in closed)):
                raise InputClosed(f"input {closed[0]!r} is closed and no group on {list(ports)} can be completed")
            return None

        return await self._wait(check, timeout, f"a group on inputs {list(ports)}", ports=ports)

    async def iter_input_messages(self, port: str) -> AsyncIterator[Message]:
        inp = self._input(port)

        def check() -> Message | object | None:
            versions = inp.after(inp.cursor)
            if versions:
                return inp.messages[versions[0]]
            return _CLOSED if inp.closed else None

        while True:
            found = await self._wait(check, None, f"a message on input {port!r}", ports=(port,))
            if found is _CLOSED:
                return
            assert isinstance(found, Message)
            inp.cursor = found.version
            yield found

    # -- outputs and the main agent -------------------------------------------

    async def publish_output_message(
        self,
        port: str,
        *,
        text: str | None = None,
        data: Mapping[str, Any] | None = None,
        refs: Iterable[Ref] = (),
    ) -> Message:
        if port not in self._outputs:
            raise NodeError(f"{port!r} is not an output port of {self._type!r}")
        refs = tuple(refs)
        for ref in refs:
            if not isinstance(ref, Ref):
                raise NodeError(f"refs must be Ref objects, got {type(ref).__name__}")
            # A ref naming this node as its producer is the node's own content: it has to be this
            # instance's and sit under one of its output ports. Refs from other nodes pass as they are.
            if ref.producer == self.node_id and (ref.producer_instance != self.instance_id or ref.port not in self._outputs):
                raise NodeError(
                    f"ref {ref.port}/{ref.key} names {self.node_id!r} as producer but not this instance "
                    f"({self.instance_id}) and one of its output ports {sorted(self._outputs)}"
                )
        result = await self._request(
            {
                "kind": Kind.PUBLISH,
                "port": port,
                "text": text or "",
                "data": None if data is None else _json_ready(data, "data"),
                "refs": [ref.model_dump(mode="json") for ref in refs],
            }
        )
        return Message.model_validate(result)

    async def report_to_main_agent(self, text: str) -> None:
        self._send({"kind": Kind.REPORT, "text": str(text)})

    async def emit_event(self, event: Event | dict[str, Any]) -> None:
        if isinstance(event, Event):
            if event.type not in NODE_EVENT_TYPES:
                raise NodeError(
                    f"a node cannot emit {event.type!r} events; use {EventType.NODE_CUSTOM!r} or the dedicated call"
                )
            event = Event(type=event.type, payload=_json_ready(event.payload, "event payload"))
        else:
            # A plain dict is the payload of a node.custom event, keys chosen by the node.
            event = Event(type=EventType.NODE_CUSTOM, payload=_json_ready(event, "event"))
        self._send({"kind": Kind.EVENT, "event": event.to_dict()})

    async def ask_main_agent(self, question: str) -> str:
        future = self._submit({"kind": Kind.ASK, "question": str(question)})
        reply = await self._wait(lambda: future.result() if future.done() else None, None, "an answer")
        return _unpack(reply)["answer"]

    async def take_main_agent_messages(self) -> tuple[MainAgentMessage, ...]:
        messages = tuple(self._main_agent_messages)
        self._main_agent_messages.clear()
        return messages

    async def wait_for_main_agent_message(self, timeout: float | None = None) -> MainAgentMessage:
        pending = self._main_agent_messages
        return await self._wait(lambda: pending.popleft() if pending else None, timeout, "a message from the main agent")

    async def set_status_snapshot(self, data: Mapping[str, Any]) -> None:
        self._send({"kind": Kind.SNAPSHOT, "data": _json_ready(data, "status snapshot")})

    def ref_dir(self, ref: Ref) -> Path:
        return ref_dir(self.workspace, self.run_id, ref)

    async def wait_for_run_permission(self) -> bool:
        while not self._stop_requested:
            if not self._paused:
                return True
            await self._signal.wait(None)
        return False

    def get_model_client(self) -> ModelClient:
        if self._model_client is None:
            # Imported here: the provider SDKs are heavy and most nodes never call a model.
            from emerge.models.factory import create_client

            self._model_client = create_client(self.config, callbacks=self.emitter)
        return self._model_client

    async def aclose(self) -> None:
        if self._model_client is not None:
            await self._model_client.aclose()

    # -- plumbing -----------------------------------------------------------

    def _input(self, port: str) -> _Input:
        inp = self._inputs.get(port)
        if inp is None:
            raise NodeError(f"{port!r} is not an input port of {self._type!r}")
        return inp

    async def _synced(self, port: str) -> _Input:
        """The input, with whatever its source port still keeps read in from Storage."""
        inp = self._input(port)
        while not inp.synced:
            source = inp.source
            result = _unpack(await self._submit({"kind": Kind.FETCH, "port": port}))
            # Rewired while the fetch was out: what came back belongs to the old source.
            if inp.source == source == result["source"]:
                for data in result["messages"]:
                    inp.add(Message.model_validate(data), self._port_buffer)
                inp.synced = True
        return inp

    async def _wait[T](
        self,
        check: Callable[[], T | None],
        timeout: float | None,
        what: str,
        *,
        ports: Sequence[str] = (),
    ) -> T:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            for port in ports:
                await self._synced(port)
            if self._stop_requested:
                raise StopRequested(f"stop requested while waiting for {what}")
            found = check()
            if found is not None:
                return found
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                raise WaitTimeout(f"no {what} within {timeout} s")
            await self._signal.wait(remaining)

    def _submit(self, message: dict[str, Any]) -> asyncio.Future[dict[str, Any]]:
        self._last_request += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._requests[self._last_request] = future
        self._send({**message, "req": self._last_request})
        return future

    async def _request(self, message: dict[str, Any]) -> Any:
        return _unpack(await self._submit(message))


def _unpack(reply: dict[str, Any]) -> Any:
    if reply["ok"]:
        return reply["result"]
    if reply["stop"]:
        raise StopRequested(reply["error"])
    raise NodeError(reply["error"])


def _json_ready(value: Any, what: str) -> Any:
    plain = thaw(value)
    try:
        json.dumps(plain)
    except (TypeError, ValueError) as e:
        raise NodeError(f"{what} must be JSON-serializable: {e}") from None
    return plain
