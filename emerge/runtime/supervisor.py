"""Starting, watching and stopping a run's node processes, and deciding when each node may start.

The supervisor starts every node without a start edge at once and every other one when
`start_status` (graph/graph.py, the one place the rule lives) says an edge fired. Each state change
goes through the bus: written to Storage, stamped, announced as `node.state`. When the current
instance of a node ends, the inputs it feeds are closed, its unanswered questions are voided, and
the nodes that now can never start are marked skipped. A node that ended is never started again.

Graph changes go through `apply_patch`: it checks a whole patch against the node states of that
moment and carries it out under the same lock, touching only the nodes and inputs the change names.
`continue_run` starts a supervisor on what Storage holds for a run whose session was closed, instead
of on a fresh graph. A whole run can be paused: nodes started while it is (by a start edge, a
commit, a replacement) start paused too.

The single-step operations further down (add, remove, rewire, replace) each adopt a whole new graph
and fit a change that touches one thing; a patch with more in it goes through `apply_patch`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from emerge.callbacks import EventEmitter
from emerge.config import WorkspaceConfig
from emerge.exceptions import RuntimeControlError
from emerge.graph.graph import Graph, NodeSpec, Replacement, StartStatus, start_status
from emerge.graph.patch import Edit, GraphDiff, GraphPatch, apply_graph_patch, diff_graphs
from emerge.graph.ports import MainAgentMessage, Ref, thaw
from emerge.graph.registry import NodeRegistry, get_active_registry
from emerge.runtime.bus import MessageBus, input_message
from emerge.runtime.policies import StopPolicy
from emerge.runtime.process import NodeLaunch, NodeProcess
from emerge.runtime.state import REPLACEABLE_STATES, LifecycleState, NodeRuntimeState
from emerge.storage.base import AskRecord, NodeState, Storage

logger = logging.getLogger(__name__)

_S = LifecycleState
# States a pause or a stop acts on: the process exists and has not been asked to stop.
_PAUSABLE = frozenset({_S.STARTING, _S.RUNNING})
_STOPPABLE = frozenset({_S.STARTING, _S.RUNNING, _S.PAUSED})
# The instance of a node that was running when its session closed. Reopening starts it anew.
_INTERRUPTED = frozenset({_S.STARTING, _S.RUNNING, _S.PAUSED})

# Called under the supervisor's lock once a new graph version has passed its checks and before any
# of it is carried out: (old graph, new graph, the patch, the difference).
OnCommit = Callable[[Graph, Graph, GraphPatch, GraphDiff], Awaitable[None]]


class NodeFate(StrEnum):
    """Where a node that had not started stands right after a graph change took effect."""

    STARTED = "started"
    WAITING = "waiting"
    # None of its start edges can fire any more; recorded as skipped.
    NEVER = "never"
    # A new instance whose previous one is still stopping; it starts once that process is gone.
    AFTER_OLD_INSTANCE = "after_old_instance"


@dataclass(frozen=True, slots=True)
class PatchExecution:
    """What `apply_patch` did. `new is old` when the patch changed nothing."""

    old: Graph
    new: Graph
    diff: GraphDiff
    # Every node that had not started when the change took effect, and where it is now.
    fates: dict[str, NodeFate] = field(default_factory=dict)
    # Nodes given a new instance whose old process was told to stop and has not exited yet.
    held: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RerunNode:
    node_id: str
    old_instance_id: str
    new_instance_id: str
    # The state the old instance was in when the session closed.
    was: LifecycleState
    started_paused: bool


@dataclass(frozen=True, slots=True)
class ContinueExecution:
    """What `continue_run` did with each node of the stored graph."""

    graph: Graph
    # Ended before the session closed; left exactly as stored.
    kept: tuple[str, ...]
    rerun: tuple[RerunNode, ...]
    # Were stopping when the session closed: recorded as stopped, not started again.
    stopped: tuple[str, ...]
    # Nodes that had not started, and where they are now.
    fates: dict[str, NodeFate]


class Supervisor:
    """Runs one run's graph: one process per started node, one bus between them and the main process.

    `storage` must be opened with the same port buffer as `config.runtime.port_buffer`, and the run
    must already exist in it. `on_idle` is called (a coroutine is scheduled) each time the run
    settles: every node of the graph has reached a terminal state and no node process is left.
    """

    def __init__(
        self,
        storage: Storage,
        callbacks: EventEmitter,
        config: WorkspaceConfig,
        registry: NodeRegistry | None = None,
        *,
        policy: StopPolicy | None = None,
        on_idle: Callable[[], Awaitable[Any] | None] | None = None,
    ) -> None:
        self.storage = storage
        self.callbacks = callbacks
        self.config = config
        self.registry = registry if registry is not None else get_active_registry()
        self.policy = policy if policy is not None else StopPolicy.from_config(config.runtime)
        self.on_idle = on_idle
        self.run_id = ""
        self._graph = Graph()
        self._bus: MessageBus | None = None
        # Every instance this run has had, by instance id; a replaced one keeps its record.
        self._records: dict[str, NodeRuntimeState] = {}
        self._acts_on_environment: dict[str, bool] = {}
        # Started instances whose process has not been reaped yet, by instance id.
        self._processes: dict[str, NodeProcess] = {}
        # Inputs whose source node was removed: closed, and without a source until rewired.
        self._orphaned: set[tuple[str, str]] = set()
        # Nodes whose new instance must not start yet: the old process is still stopping.
        self._holding: set[str] = set()
        # The whole run is paused: every node started meanwhile starts paused.
        self._run_paused = False
        # Instances to start paused although the run is not: the instance they replace was paused.
        self._start_paused: set[str] = set()
        # Instances started paused that have not been resumed yet; ready, they go to paused.
        self._launch_paused: set[str] = set()
        self._ops = asyncio.Lock()
        self._idle = asyncio.Event()
        self._shutting_down = False
        # Session closing: processes stop, but nothing about it is recorded or passed on.
        self._closing = False
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- queries --------------------------------------------------------------

    @property
    def graph(self) -> Graph:
        return self._graph

    @property
    def bus(self) -> MessageBus:
        if self._bus is None:
            raise RuntimeControlError("the supervisor has not started a run")
        return self._bus

    @property
    def idle(self) -> bool:
        return self._idle.is_set()

    @property
    def run_paused(self) -> bool:
        return self._run_paused

    def ended_outcomes(self) -> dict[str, str | None]:
        """Every node whose current instance has ended, with its outcome (None unless finished)."""
        return self._ended()

    def state(self, node_id: str) -> NodeRuntimeState | None:
        """The current instance's record."""
        return self._current(node_id)

    def states(self) -> dict[str, NodeRuntimeState]:
        return {spec.id: self._records[spec.instance_id] for spec in self._graph.nodes if spec.instance_id in self._records}

    def instance_states(self) -> tuple[NodeRuntimeState, ...]:
        """Every instance the run has had, replaced ones included."""
        return tuple(self._records.values())

    async def wait_idle(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._idle.wait(), timeout)

    async def status_snapshot(self, node_id: str) -> Any | None:
        record = self._require_current(node_id)
        return await self.storage.node_snapshot(self.run_id, node_id, record.instance_id)

    # -- running --------------------------------------------------------------

    async def start(self, graph: Graph, run_id: str) -> None:
        """Register every node of `graph` as pending and start the ones that may start now."""
        if self._bus is not None:
            raise RuntimeControlError("this supervisor has already started a run")
        self.run_id = run_id
        bus = MessageBus(run_id, self.storage, self.callbacks, port_buffer=self.config.runtime.port_buffer)
        bus.lifecycle = self
        await bus.start()
        self._bus = bus
        async with self._ops:
            self._set_graph(graph)
            for spec in graph.nodes:
                await self._register(spec)
            await self._advance()

    async def continue_run(
        self,
        graph: Graph,
        run_id: str,
        stored: Iterable[NodeState],
        *,
        paused: bool = False,
        on_commit: OnCommit | None = None,
    ) -> ContinueExecution:
        """Carry on a run whose session was closed, from the node states Storage kept for it.

        A node that had ended stays as it is, outcome included, and its record is not rewritten: its
        outcome counts for start edges as before, and inputs it fed are closed. A node that was
        running (starting, running, paused) gets a new instance at the next graph version, through
        a replacement, and starts from scratch; its old instance is recorded as stopped, which, as
        for any replaced instance, fires no start edge and closes no input. A node that was
        stopping is recorded as stopped and not started again. A node that had not started waits
        as before and starts at once if its start edges allow. With `paused` the run continues
        paused, and so does a node that was paused when the session closed.
        """
        if self._bus is not None:
            raise RuntimeControlError("this supervisor has already started a run")
        self.run_id = run_id
        bus = MessageBus(run_id, self.storage, self.callbacks, port_buffer=self.config.runtime.port_buffer)
        bus.lifecycle = self
        await bus.start()
        self._bus = bus
        async with self._ops:
            self._run_paused = paused
            self._set_graph(graph)
            # Type and pinned code of every instance the run had, to know which ones act on the environment.
            kinds = {record.old_instance_id: (record.old_type, record.old_code) for record in graph.replacements}
            kinds.update({spec.instance_id: (spec.type, spec.code) for spec in graph.nodes})
            for row in stored:
                self._records[row.instance_id] = NodeRuntimeState(
                    node_id=row.node_id,
                    instance_id=row.instance_id,
                    state=LifecycleState(row.state),
                    outcome=row.outcome,
                    reason=row.reason,
                    error=row.error,
                    changed_at=row.updated_at,
                )
                kind = kinds.get(row.instance_id)
                entry = self.registry.find_entry(*kind) if kind is not None else None
                self._acts_on_environment[row.instance_id] = entry is not None and entry.acts_on_environment
            # A replaced instance that was still stopping when the session closed has stopped now.
            for record in list(self._records.values()):
                if record.state in _INTERRUPTED | {_S.STOPPING} and not graph.is_current(record.node_id, record.instance_id):
                    await self._set_state(
                        record.instance_id, _S.STOPPED, error=f"the session was closed while this replaced instance was {record.state}"
                    )
            kept: list[str] = []
            stopped: list[str] = []
            interrupted: list[tuple[str, LifecycleState]] = []
            for spec in graph.nodes:
                record = self._records.get(spec.instance_id)
                if record is None:
                    await self._register(spec)
                elif record.terminal:
                    kept.append(spec.id)
                elif record.state is _S.STOPPING:
                    await self._set_state(
                        spec.instance_id, _S.STOPPED, error="the session was closed while the node was stopping"
                    )
                    stopped.append(spec.id)
                elif record.state in _INTERRUPTED:
                    interrupted.append((spec.id, record.state))
            rerun: list[RerunNode] = []
            if interrupted:
                old = graph
                # A new instance each, on the code version the old one was pinned to.
                patch = GraphPatch(
                    edits=tuple(
                        Edit(op="replace_node", node=node_id, code=old.node(node_id).code)  # type: ignore[union-attr]
                        for node_id, _ in interrupted
                    )
                )
                states = {record.node_id: str(record.state) for record in self._current_records()}
                new = apply_graph_patch(old, patch, self.registry, states=states)
                diff = diff_graphs(old, new)
                if on_commit is not None:
                    await on_commit(old, new, patch, diff)
                self._set_graph(new)
                for node_id, was in interrupted:
                    old_spec, new_spec = old.node(node_id), new.node(node_id)
                    assert old_spec is not None and new_spec is not None
                    await self._set_state(
                        old_spec.instance_id,
                        _S.STOPPED,
                        error=f"the session was closed while this instance was {was}; a new instance runs instead",
                    )
                    await self._register(new_spec)
                    if was is _S.PAUSED:
                        self._start_paused.add(new_spec.instance_id)
                    rerun.append(
                        RerunNode(
                            node_id=node_id,
                            old_instance_id=old_spec.instance_id,
                            new_instance_id=new_spec.instance_id,
                            was=was,
                            started_paused=paused or was is _S.PAUSED,
                        )
                    )
            waiting = self._pending_ids()
            await self._advance()
            return ContinueExecution(
                graph=self._graph,
                kept=tuple(kept),
                rerun=tuple(rerun),
                stopped=tuple(stopped),
                fates={node_id: self._fate(node_id) for node_id in waiting if node_id not in {r.node_id for r in rerun}},
            )

    async def stop(self, node_id: str) -> NodeRuntimeState:
        """Ask the node to stop and wait for it; past `stop_timeout` its process is killed."""
        async with self._ops:
            record = self._require_current(node_id)
            if record.terminal:
                return record
            if record.state is _S.PENDING:
                raise RuntimeControlError(f"node {node_id!r} has not started; remove it instead of stopping it")
            process = self._processes[record.instance_id]
            await self._request_stop(process)
        await self._await_exit(process)
        return self._records[process.instance_id]

    async def pause(self, node_id: str) -> NodeRuntimeState:
        """The node blocks at its next `wait_for_run_permission()`; nothing in progress is cut off."""
        async with self._ops:
            record = self._require_current(node_id)
            if record.state is not _S.PAUSED:
                if record.state not in _PAUSABLE:
                    raise RuntimeControlError(f"node {node_id!r} is {record.state}; only a starting or running node can be paused")
                await self._pause(record)
            return self._records[record.instance_id]

    async def resume(self, node_id: str) -> NodeRuntimeState:
        async with self._ops:
            record = self._require_current(node_id)
            if record.state is _S.PAUSED:
                await self._resume(record)
            elif record.state not in _PAUSABLE:
                raise RuntimeControlError(f"node {node_id!r} is {record.state}; only a paused node can be resumed")
            elif record.instance_id in self._launch_paused:
                self._release_paused_launch(record)
            return self._records[record.instance_id]

    async def pause_all(self) -> None:
        """Pause the whole run: every running node now, and every node that starts until `resume_all`."""
        async with self._ops:
            self._run_paused = True
            for record in self._current_records():
                if record.state in _PAUSABLE:
                    await self._pause(record)

    async def resume_all(self) -> None:
        async with self._ops:
            self._run_paused = False
            self._start_paused.clear()
            for record in self._current_records():
                if record.state is _S.PAUSED:
                    await self._resume(record)
                elif record.instance_id in self._launch_paused:
                    self._release_paused_launch(record)

    async def stop_all(self) -> None:
        started = [record.node_id for record in self._current_records() if record.state in _STOPPABLE]
        await asyncio.gather(*(self.stop(node_id) for node_id in started))

    async def send_to_node(
        self,
        node_id: str,
        *,
        text: str | None = None,
        data: Mapping[str, Any] | None = None,
        refs: Iterable[Ref] = (),
    ) -> MainAgentMessage:
        """Deliver a main-agent message to the node's current instance; refused once the node has ended."""
        _, message, _ = await self.deliver(node_id, text=text, data=data, refs=refs)
        return message

    async def deliver(
        self,
        node_id: str,
        *,
        text: str | None = None,
        data: Mapping[str, Any] | None = None,
        refs: Iterable[Ref] = (),
    ) -> tuple[NodeRuntimeState, MainAgentMessage, bool]:
        """`send_to_node`, also telling which instance got it and whether it waits for that one to start."""
        async with self._ops:
            record = self._require_current(node_id)
            if record.terminal:
                raise RuntimeControlError(f"node {node_id!r} is {record.state}; the message was not delivered")
            queued = self.bus.current_process(node_id) is None
            message = await self.bus.deliver_main_agent_message(
                node_id, record.instance_id, self._acts_on_environment[record.instance_id], text=text, data=data, refs=refs
            )
            return record, message, queued

    async def answer_ask(self, ask_id: str, answer: str) -> AskRecord:
        return await self.bus.answer_ask(ask_id, answer)

    async def shutdown(self, *, closing: bool = False) -> None:
        """Stop every node process and close the bus.

        With `closing=True` (the session is being closed) the processes stop the same way, but none
        of it is recorded: Storage keeps each node in the state it had before, no start edge fires,
        no input closes and nothing is announced, so reopening the session can carry on from there.
        """
        if self._bus is None:
            return
        async with self._ops:
            self._shutting_down = True
            self._closing = closing
            processes = [process for process in self._processes.values() if not process.exited.is_set()]
            for process in processes:
                await self._request_stop(process)
        await asyncio.gather(*(self._await_exit(process) for process in processes))
        await asyncio.gather(*self._tasks)
        await self._bus.close()

    # -- graph changes ----------------------------------------------------------

    async def apply_patch(self, patch: GraphPatch, *, on_commit: OnCommit | None = None) -> PatchExecution:
        """Check `patch` against the node states of this moment and carry it out, all under the lock.

        Nothing can change state between the check and the work: if any edit is refused, the graph
        stays at its version and no process is started or stopped (GraphValidationError). Only what
        the difference names is touched; a running node it does not name keeps its process and
        instance. A replaced node's old process is asked to stop and the call returns without
        waiting for it; the new instance starts once that process is gone, paused if the old one
        was. Every input whose source changed hears of it once, about its source in the new graph.
        Nodes added are all registered before start edges are judged, once for the whole patch.
        """
        async with self._ops:
            if self._shutting_down:
                raise RuntimeControlError("the run is shutting down; the graph can no longer change")
            old = self._graph
            states = {record.node_id: str(record.state) for record in self._current_records()}
            new = apply_graph_patch(old, patch, self.registry, states=states)
            if new is old:
                return PatchExecution(old=old, new=old, diff=GraphDiff())
            diff = diff_graphs(old, new)
            if on_commit is not None:
                await on_commit(old, new, patch, diff)
            self._set_graph(new)
            # Before anything awaits: a message the bus routes by the new graph must reach a node
            # after it has heard of its new source.
            self._renotify_inputs(old, new, diff)
            for node_id in diff.nodes.removed:
                spec = old.node(node_id)
                assert spec is not None
                # Never started, never will: the record ends here, under its own instance id.
                await self._set_state(spec.instance_id, _S.SKIPPED)
            held: list[str] = []
            for record in diff.nodes.replaced:
                if await self._swap_instance(record, new):
                    held.append(record.node)
            for node_id in diff.nodes.added:
                spec = new.node(node_id)
                assert spec is not None
                await self._register(spec)
            waiting = self._pending_ids()
            await self._advance()
            return PatchExecution(
                old=old,
                new=new,
                diff=diff,
                fates={node_id: self._fate(node_id) for node_id in waiting},
                held=tuple(held),
            )

    async def add_node(self, graph: Graph, node_id: str) -> StartStatus:
        """Adopt `graph`, which adds `node_id`; the node starts at once if it may.

        Returns what its start edges said at that moment: READY (started), WAITING, or NEVER
        (recorded as skipped).
        """
        async with self._ops:
            spec = graph.node(node_id)
            if spec is None or self._graph.node(node_id) is not None:
                raise RuntimeControlError(f"{node_id!r} is not a node added by this graph")
            self._set_graph(graph)
            await self._register(spec)
            verdict = start_status(graph, self._ended())[node_id]
            await self._advance()
            return verdict

    async def remove_node(self, graph: Graph, node_id: str) -> None:
        """Adopt `graph`, which no longer holds `node_id`. Only a node that never started can go."""
        async with self._ops:
            record = self._require_current(node_id)
            if record.state is not _S.PENDING:
                raise RuntimeControlError(f"node {node_id!r} is {record.state}; only a pending node can be removed, stop it instead")
            if graph.node(node_id) is not None:
                raise RuntimeControlError(f"the graph given still holds {node_id!r}")
            fed = [sub for sub in self._graph.subscriptions if sub.source_node == node_id]
            self._set_graph(graph)
            # Never started, never will: the record ends here, under its own instance id.
            await self._set_state(record.instance_id, _S.SKIPPED)
            for sub in fed:
                self._orphaned.add((sub.target_node, sub.target_port))
                target = self.bus.current_process(sub.target_node)
                if target is not None:
                    target.send(input_message(sub.target_port, None, True))
            await self._advance()

    async def rewire(self, graph: Graph, node_id: str, port: str) -> None:
        """Adopt `graph`, in which input `node_id.port` reads from a different output (or none).

        The node goes on with what the new source publishes from now on; a wait or an iteration in
        progress does not end. The input is closed if the new source has ended.
        """
        async with self._ops:
            if graph.node(node_id) is None:
                raise RuntimeControlError(f"no node {node_id!r} in the graph given")
            # No await between adopting the graph and telling the node: a message the bus routes by
            # the new graph must reach the node after it has heard of the new source.
            self._set_graph(graph)
            self._orphaned.discard((node_id, port))
            target = self.bus.current_process(node_id)
            if target is not None:
                target.send(input_message(port, **self._input_state(node_id, port, from_now=True)))

    async def update_start_edges(self, graph: Graph, node_id: str) -> StartStatus | None:
        """Adopt `graph`, in which a pending node's start edges changed, and check them right away.

        Against the outcomes that have already happened: an edge already satisfied fires now, and
        a node none of whose edges can fire any more is recorded as skipped. Returns the verdict.
        """
        async with self._ops:
            self._set_graph(graph)
            verdict = start_status(graph, self._ended()).get(node_id)
            await self._advance()
            return verdict

    async def update_graph(self, graph: Graph) -> None:
        """Adopt a version whose changes need no process started or stopped, e.g. a description."""
        async with self._ops:
            self._set_graph(graph)
            await self._advance()

    async def replace_instance(
        self, node_id: str, new_spec: NodeSpec, *, graph: Graph | None = None
    ) -> NodeRuntimeState:
        """Make `new_spec` the node's instance: register it, stop the old process, then start it.

        `graph` is the committed version holding `new_spec`; without one, the current graph with the
        spec swapped in is used. The old process ends by the ordinary stop procedure, and how it ends
        is recorded under the old instance only: its subscribers' inputs stay open and nothing waiting
        on the node hears of it. The new instance starts once the old process is gone, if the node
        had started; a pending node only changes its registration.
        """
        async with self._ops:
            old = self._require_current(node_id)
            if old.state not in REPLACEABLE_STATES:
                raise RuntimeControlError(
                    f"node {node_id!r} is {old.state}; only {sorted(str(s) for s in REPLACEABLE_STATES)} nodes can be replaced"
                )
            if new_spec.id != node_id or new_spec.instance_id == old.instance_id:
                raise RuntimeControlError("a replacement keeps the node id and brings a new instance id")
            if graph is None:
                graph = self._graph.model_copy(
                    update={"nodes": tuple(new_spec if spec.id == node_id else spec for spec in self._graph.nodes)}
                )
            elif graph.node(node_id) != new_spec:
                raise RuntimeControlError(f"the graph given does not hold {new_spec.instance_id} for {node_id!r}")
            # Current before the old process hears anything, so whatever it sends from here on is
            # stale and dropped.
            self._set_graph(graph)
            await self._register(new_spec)
            old_process = self._processes.get(old.instance_id)
            if old_process is not None:
                self._holding.add(node_id)
                await self._request_stop(old_process)
            else:
                await self._set_state(old.instance_id, _S.SKIPPED)
        if old_process is not None:
            try:
                await self._await_exit(old_process)
            finally:
                self._holding.discard(node_id)
        async with self._ops:
            await self._advance()
            return self._records[new_spec.instance_id]

    # -- from the bus -----------------------------------------------------------

    async def on_node_ready(self, process: NodeProcess) -> None:
        async with self._ops:
            if self._records[process.instance_id].state is _S.STARTING:
                if process.instance_id in self._launch_paused:
                    # Started paused: setup() is done and run() holds at its first permission check.
                    self._launch_paused.discard(process.instance_id)
                    await self._set_state(process.instance_id, _S.PAUSED)
                else:
                    await self._set_state(process.instance_id, _S.RUNNING)

    async def on_node_exit(self, process: NodeProcess, report: dict[str, Any]) -> None:
        process.exit_report = report
        async with self._ops:
            await self._finish(process)

    async def on_node_disconnected(self, process: NodeProcess) -> None:
        if process.exit_report is None:
            # Gone without a word: give it a moment to be reaped, so the error can carry the exit code.
            await process.join(1.0)
        async with self._ops:
            self.bus.detach(process)
            await self._finish(process)
        self._spawn(self._reap(process))

    # -- internals --------------------------------------------------------------

    def _set_graph(self, graph: Graph) -> None:
        self._graph = graph
        self.bus.graph = graph

    def _current(self, node_id: str) -> NodeRuntimeState | None:
        spec = self._graph.node(node_id)
        return None if spec is None else self._records.get(spec.instance_id)

    def _require_current(self, node_id: str) -> NodeRuntimeState:
        record = self._current(node_id)
        if record is None:
            raise RuntimeControlError(f"no node {node_id!r} in the graph")
        return record

    def _current_records(self) -> list[NodeRuntimeState]:
        return [record for spec in self._graph.nodes if (record := self._records.get(spec.instance_id)) is not None]

    def _ended(self) -> dict[str, str | None]:
        # Every node whose current instance is over, with its outcome; None for all but finished ones.
        return {record.node_id: record.outcome for record in self._current_records() if record.terminal}

    def _input_state(self, node_id: str, port: str, *, from_now: bool = False) -> dict[str, Any]:
        """What an input looks like now. `from_now`: it was just rewired, and reads only what its source
        publishes from here on."""
        sub = self._graph.source_of(node_id, port)
        if sub is None:
            return {"source": None, "closed": (node_id, port) in self._orphaned, "start": None}
        # A source added by the change being carried out has no record yet; it is open.
        source = self._current(sub.source_node)
        return {
            "source": f"{sub.source_node}.{sub.source_port}",
            "closed": source is not None and source.terminal,
            "start": self.bus.latest_version(sub.source_node, sub.source_port) if from_now else None,
        }

    def _renotify_inputs(self, old: Graph, new: Graph, diff: GraphDiff) -> None:
        """Tell each running node about every input of its whose source changed, once per input."""
        removed = set(diff.nodes.removed)
        # Input -> the subscription it had before, None if it had none.
        changed: dict[tuple[str, str], Any] = {}
        for sub in diff.subscriptions.added:
            changed.setdefault((sub.target_node, sub.target_port), None)
        for sub in diff.subscriptions.removed:
            changed[(sub.target_node, sub.target_port)] = sub
        for before, _ in diff.subscriptions.rewired:
            changed[(before.target_node, before.target_port)] = before
        for (node_id, port), before in changed.items():
            if new.node(node_id) is None:
                continue
            if new.source_of(node_id, port) is None and before is not None and before.source_node in removed:
                # Its source went away and nothing replaced it: closed, until it is wired again.
                self._orphaned.add((node_id, port))
            else:
                self._orphaned.discard((node_id, port))
            target = self.bus.current_process(node_id)
            if target is not None:
                target.send(input_message(port, **self._input_state(node_id, port, from_now=True)))

    async def _swap_instance(self, record: Replacement, graph: Graph) -> bool:
        """Put the new instance of a replaced node in place; True if it waits for the old process."""
        old = self._records[record.old_instance_id]
        spec = graph.node(record.node)
        assert spec is not None
        await self._register(spec)
        process = self._processes.get(old.instance_id)
        if process is None or process.exited.is_set():
            # It never started: only the registration changes.
            await self._set_state(old.instance_id, _S.SKIPPED)
            return False
        if old.state is _S.PAUSED or old.instance_id in self._launch_paused:
            self._start_paused.add(spec.instance_id)
        self._holding.add(record.node)
        await self._request_stop(process)
        self._spawn(self._start_after_exit(record.node, process))
        return True

    async def _start_after_exit(self, node_id: str, process: NodeProcess) -> None:
        # Never two processes for one node: the new instance waits for the old one to be gone,
        # which takes at most stop_timeout before it is killed.
        try:
            await self._await_exit(process)
        finally:
            async with self._ops:
                self._holding.discard(node_id)
                await self._advance()

    def _pending_ids(self) -> list[str]:
        return [record.node_id for record in self._current_records() if record.state is _S.PENDING]

    def _fate(self, node_id: str) -> NodeFate:
        record = self._require_current(node_id)
        if record.state is _S.PENDING:
            return NodeFate.AFTER_OLD_INSTANCE if node_id in self._holding else NodeFate.WAITING
        if record.state is _S.SKIPPED:
            return NodeFate.NEVER
        return NodeFate.STARTED

    async def _register(self, spec: NodeSpec) -> None:
        entry = self.registry.find_entry(spec.type, spec.code)
        self._acts_on_environment[spec.instance_id] = entry is not None and entry.acts_on_environment
        record = NodeRuntimeState(node_id=spec.id, instance_id=spec.instance_id)
        await self.bus.record_state(
            record,
            previous=None,
            acts_on_environment=self._acts_on_environment[spec.instance_id],
            current=self._graph.is_current(spec.id, spec.instance_id),
        )
        self._records[spec.instance_id] = record
        self._idle.clear()

    async def _set_state(self, instance_id: str, state: LifecycleState, **fields: Any) -> None:
        if self._closing:
            return
        old = self._records[instance_id]
        new = old.transition(state, **fields)
        await self.bus.record_state(
            new,
            previous=old.state,
            acts_on_environment=self._acts_on_environment[instance_id],
            current=self._graph.is_current(new.node_id, instance_id),
        )
        self._records[instance_id] = new

    async def _advance(self) -> None:
        """Apply `start_status`: skip what can never start, start what may start now."""
        status = start_status(self._graph, self._ended())
        for node_id, verdict in status.items():
            record = self._current(node_id)
            if record is not None and record.state is _S.PENDING and verdict is StartStatus.NEVER:
                await self._set_state(record.instance_id, _S.SKIPPED)
                self._close_inputs_fed_by(node_id)
        if not self._shutting_down:
            for node_id, verdict in status.items():
                record = self._current(node_id)
                # Checked again for each: starting one node can end another (e.g. a failed launch).
                if (
                    record is not None
                    and record.state is _S.PENDING
                    and verdict is StartStatus.READY
                    and node_id not in self._holding
                ):
                    await self._launch(node_id)
        self._check_idle()

    async def _launch(self, node_id: str) -> None:
        spec = self._graph.node(node_id)
        assert spec is not None
        instance_id = spec.instance_id
        paused = self._run_paused or instance_id in self._start_paused
        self._start_paused.discard(instance_id)
        try:
            entry = self.registry.require_entry(spec.type, spec.code)
            launch = NodeLaunch(
                run_id=self.run_id,
                node_id=node_id,
                instance_id=instance_id,
                node_type=spec.type,
                locator=entry.locator.to_dict(),
                params=thaw(spec.params),
                description=spec.description,
                inputs={port.name: self._input_state(node_id, port.name) for port in entry.inputs},
                workspace=str(self.config.workspace),
                session_dir=str(self.storage.session_dir),
                config=self.config.model_dump(mode="json"),
                port_buffer=self.config.runtime.port_buffer,
                paused=paused,
            )
            process = NodeProcess(launch, acts_on_environment=entry.acts_on_environment)
            await process.start(self.bus.post)
        except Exception as e:
            # An unknown type or a process that could not be spawned: nothing of the node ran.
            await self._set_state(instance_id, _S.STARTING)
            await self._set_state(instance_id, _S.FAILED, error=f"cannot start: {type(e).__name__}: {e}")
            await self._on_current_ended(node_id)
            return
        self._processes[instance_id] = process
        if paused:
            self._launch_paused.add(instance_id)
        await self.bus.attach(process)
        await self._set_state(instance_id, _S.STARTING, pid=process.pid)

    async def _pause(self, record: NodeRuntimeState) -> None:
        self._launch_paused.discard(record.instance_id)
        await self._set_state(record.instance_id, _S.PAUSED)
        self._processes[record.instance_id].control("pause")

    def _release_paused_launch(self, record: NodeRuntimeState) -> None:
        # Started paused and not ready yet: it stays starting, and goes on to running once ready.
        self._launch_paused.discard(record.instance_id)
        self._processes[record.instance_id].control("resume")

    async def _resume(self, record: NodeRuntimeState) -> None:
        await self._set_state(record.instance_id, _S.RUNNING)
        self._processes[record.instance_id].control("resume")

    async def _request_stop(self, process: NodeProcess) -> None:
        process.stop_requested = True
        if self._records[process.instance_id].state in _STOPPABLE:
            await self._set_state(process.instance_id, _S.STOPPING)
        process.control("stop")

    async def _await_exit(self, process: NodeProcess) -> None:
        try:
            await asyncio.wait_for(process.exited.wait(), self.policy.stop_timeout)
        except TimeoutError:
            # It never reached a point where it could stop.
            process.kill()
            await process.exited.wait()

    async def _finish(self, process: NodeProcess) -> None:
        """Record how a process ended, once; for the current instance, pass it on."""
        if self._closing:
            return
        instance_id = process.instance_id
        if self._records[instance_id].terminal:
            return
        state, outcome, reason, error = self._final_state(process)
        await self._set_state(instance_id, state, outcome=outcome, reason=reason, error=error)
        await self.bus.void_asks(instance_id)
        if self._graph.is_current(process.node_id, instance_id):
            await self._on_current_ended(process.node_id)

    def _final_state(self, process: NodeProcess) -> tuple[LifecycleState, str | None, str, str | None]:
        # The same rules for every node: `acts_on_environment` is only a mark, it does not
        # change how an end is recorded.
        report = process.exit_report
        if process.stop_requested:
            # However run() finished, an instance asked to stop ends stopped; an outcome it returned
            # anyway is only logged.
            if report is not None:
                if report.get("returned") is not None:
                    logger.info(
                        "%s returned %r after a stop was requested; recorded as stopped, outcome dropped",
                        process.instance_id,
                        report["returned"],
                    )
                return _S.STOPPED, None, "", report.get("error")
            if process.forced:
                return _S.STOPPED, None, "", f"ended by force {self.policy.stop_timeout} s after the stop request"
            return _S.STOPPED, None, "", "the process ended after the stop request without reporting how"
        if report is None:
            return _S.FAILED, None, "", f"the node process exited unexpectedly (exit code {process.exitcode})"
        if report["result"] == "finished":
            return _S.FINISHED, report["outcome"], report["reason"], None
        if report["result"] == "stopped":
            return _S.STOPPED, None, "", report.get("error")
        return _S.FAILED, None, "", report.get("error")

    async def _on_current_ended(self, node_id: str) -> None:
        self._close_inputs_fed_by(node_id)
        await self._advance()

    def _close_inputs_fed_by(self, node_id: str) -> None:
        for sub in self._graph.subscriptions:
            if sub.source_node == node_id:
                target = self.bus.current_process(sub.target_node)
                if target is not None:
                    target.send(input_message(sub.target_port, f"{node_id}.{sub.source_port}", True))

    def _check_idle(self) -> None:
        # Idle means settled: every node's current instance has ended and every node process is
        # gone, replaced instances still stopping included.
        if self._shutting_down:
            return
        records = self._current_records()
        settled = all(record.terminal for record in records) and all(p.exited.is_set() for p in self._processes.values())
        if not records or not settled:
            self._idle.clear()
            return
        if self._idle.is_set():
            return
        self._idle.set()
        if self.on_idle is not None:
            result = self.on_idle()
            if inspect.isawaitable(result):
                self._spawn(result)

    async def _reap(self, process: NodeProcess) -> None:
        await process.reap()
        if self._processes.get(process.instance_id) is process:
            del self._processes[process.instance_id]
        self._check_idle()

    def _spawn(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
