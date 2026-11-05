"""`RuntimeService`: the one way into the runtime for the main agent's tools, the TUI and headless use.

One service serves one conversation: its storage is the conversation's, and everything its runs
produce goes under the conversation's folder. It holds the runs open in this process, each with its
own supervisor, and the callback handlers shared by all of them: the JSONL tracer (once per callback
manager, every event of every run, model tokens included), the feedback handler that reaches the
main agent, and the stream handler outside callers subscribe to. Explicit pause and stop requests
are carried out directly; no model decides on them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from emerge.callbacks import CallbackManager, JsonlTracer
from emerge.config import WorkspaceConfig
from emerge.events import EventType
from emerge.exceptions import RuntimeControlError
from emerge.graph.graph import Graph, Replacement
from emerge.graph.patch import GraphPatch
from emerge.graph.ports import Message, Ref, thaw
from emerge.graph.registry import NodeRegistry, get_active_registry
from emerge.graph.spec import graph_spec_from_graph
from emerge.runtime import checkpoint, mutations
from emerge.runtime.checkpoint import ResumeReport
from emerge.runtime.feedback import Feedback, FeedbackHandler
from emerge.runtime.mutations import GraphCommitResult, GraphFileStatus
from emerge.runtime.policies import StopPolicy
from emerge.runtime.run import Run, RunHandle, create_run
from emerge.runtime.state import NodeRuntimeState
from emerge.runtime.stream import StreamHandler, Subscription
from emerge.runtime.supervisor import Supervisor
from emerge.storage.artifacts import node_dir, trace_file
from emerge.storage.base import AskRecord, RunRecord, Storage


class NodeView(BaseModel):
    """One node of a run as `status` lists it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    instance_id: str
    type: str
    description: str = ""
    state: str
    outcome: str | None = None
    reason: str = ""
    error: str | None = None
    acts_on_environment: bool = False


class RunStatusReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: Run
    nodes: tuple[NodeView, ...]
    pending_asks: tuple[AskRecord, ...] = ()
    graph_file: GraphFileStatus


class NodeDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node: NodeView
    params: dict[str, Any]
    code: str | None = None
    # The latest status snapshot the current instance set, if any.
    snapshot: Any = None
    # Output port -> the newest message on it.
    outputs: dict[str, Message] = {}
    # runs/<run_id>/nodes/<node_id>/<instance_id>/, relative to the conversation's folder.
    artifacts_dir: str
    # Earlier instances of the node, oldest first, and how each one ended.
    replacements: tuple[Replacement, ...] = ()
    instances: tuple[NodeRuntimeState, ...] = ()
    pending_asks: tuple[AskRecord, ...] = ()


class NodeDelivery(BaseModel):
    """Where a main-agent message went."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    instance_id: str
    seq: int
    content_time: int
    # The instance has not started: it gets the message, kept in Storage until then, when it does.
    queued: bool


class RuntimeService:
    """Start, change, control, inspect, close and reopen runs. Create it with `open()`."""

    def __init__(
        self,
        storage: Storage,
        config: WorkspaceConfig,
        registry: NodeRegistry | None = None,
        *,
        callbacks: CallbackManager | None = None,
        trace: bool = True,
        on_feedback: Callable[[Feedback], Awaitable[Any] | Any] | None = None,
        feedback_interval: float = 1.0,
        summary_chars: int = 4000,
        policy: StopPolicy | None = None,
    ) -> None:
        self.storage = storage
        self.config = config
        self.session_dir = storage.session_dir
        self.registry = registry if registry is not None else get_active_registry()
        self.policy = policy
        self._owns_callbacks = callbacks is None
        self.callbacks = callbacks if callbacks is not None else CallbackManager()
        # One tracer per callback manager: it already writes every run's events to its own file.
        # A caller sharing a manager that has one passes trace=False.
        self.tracer = JsonlTracer(partial(trace_file, self.session_dir)) if trace else None
        self.feedback = FeedbackHandler(on_feedback, interval=feedback_interval, max_chars=summary_chars)
        self.streams = StreamHandler(self._values)
        self._runs: dict[str, RunHandle] = {}

    @classmethod
    async def open(
        cls,
        storage: Storage,
        config: WorkspaceConfig,
        registry: NodeRegistry | None = None,
        **options: Any,
    ) -> RuntimeService:
        service = cls(storage, config, registry, **options)
        # Handlers bind to the running event loop, so they are added here rather than in __init__.
        for handler in (service.tracer, service.feedback, service.streams):
            if handler is not None:
                service.callbacks.add_handler(handler)
        return service

    async def aclose(self) -> None:
        """Close every run still open, as when the session closes, then let go of the handlers."""
        for handle in list(self._runs.values()):
            if not handle.ended:
                await self.close_run(handle.run_id)
        if self._owns_callbacks:
            await self.callbacks.aclose()
        else:
            for handler in (self.tracer, self.feedback, self.streams):
                if handler is not None:
                    self.callbacks.remove_handler(handler)

    # -- runs -----------------------------------------------------------------

    async def start_run(self, graph: Graph) -> Run:
        """Create a run and start `graph`, which may be empty: the main agent then draws it in the file."""
        record = await create_run(self.storage)
        handle = self._make_handle(record)
        self._runs[record.id] = handle
        await self.storage.save_graph(record.id, graph)
        handle.emit(EventType.RUN_STARTED, {"graph_version": graph.version})
        await handle.supervisor.start(graph, record.id)
        await mutations.write_graph_file(handle, force=True)
        await handle.refresh_status()
        return handle.snapshot()

    async def end_run(self, run_id: str) -> Run:
        """End the run for good: nodes still running are stopped, and it is `finished`."""
        return await self._end(self._handle(run_id), "finished")

    async def close_run(self, run_id: str) -> Run:
        """The session is closing: stop the processes, keep the states; `resume_run` carries on."""
        handle = self._handle(run_id)
        if handle.ended:
            raise RuntimeControlError(f"run {run_id} is {handle.status}")
        await checkpoint.close(handle)
        del self._runs[run_id]
        self.feedback.forget(run_id)
        return handle.snapshot()

    async def resume_run(self, run_id: str) -> ResumeReport:
        """Carry on a run closed with its session; called when the session is opened again."""
        if run_id in self._runs:
            raise RuntimeControlError(f"run {run_id} is already open")
        record = await self.storage.get_run(run_id)
        if record is None:
            raise RuntimeControlError(f"no run {run_id!r}")
        handle, report = await checkpoint.resume(record, self._make_handle, self.registry)
        self._runs[run_id] = handle
        pending = await self.storage.pending_asks(run_id)
        self.feedback.seed(
            run_id,
            status=handle.status,
            nodes={
                node_id: {**state.model_dump(include={"instance_id", "outcome", "reason", "error"}), "state": str(state.state)}
                for node_id, state in handle.supervisor.states().items()
            },
            asks=[(ask.id, ask.node_id, ask.instance_id, ask.question) for ask in pending],
        )
        return report

    def runs(self) -> tuple[Run, ...]:
        """The runs this service holds, ended ones included."""
        return tuple(handle.snapshot() for handle in self._runs.values())

    # -- changing the graph -----------------------------------------------------

    async def apply_patch(self, run_id: str, patch: GraphPatch) -> GraphCommitResult:
        return await mutations.apply(self._open_handle(run_id), patch)

    async def commit_graph_file(
        self, run_id: str, *, replace: Iterable[str] = (), dry_run: bool = False
    ) -> GraphCommitResult:
        return await mutations.commit_graph_file(self._open_handle(run_id), replace=replace, dry_run=dry_run)

    async def reset_graph_file(self, run_id: str) -> GraphFileStatus:
        handle = self._handle(run_id)
        await mutations.reset_graph_file(handle)
        return mutations.graph_file_status(handle)

    def graph_file_status(self, run_id: str) -> GraphFileStatus:
        return mutations.graph_file_status(self._handle(run_id))

    # -- control ----------------------------------------------------------------

    async def pause(self, run_id: str, node_id: str | None = None) -> Run | NodeRuntimeState:
        """Pause one node, or the whole run: then nodes that start meanwhile start paused too."""
        handle = self._open_handle(run_id)
        if node_id is not None:
            return await handle.supervisor.pause(node_id)
        await handle.supervisor.pause_all()
        await handle.set_paused(True)
        return handle.snapshot()

    async def resume(self, run_id: str, node_id: str | None = None) -> Run | NodeRuntimeState:
        handle = self._open_handle(run_id)
        if node_id is not None:
            return await handle.supervisor.resume(node_id)
        await handle.supervisor.resume_all()
        await handle.set_paused(False)
        return handle.snapshot()

    async def stop(self, run_id: str, node_id: str | None = None) -> Run | NodeRuntimeState:
        """Stop one node, or the whole run, which then ends as `stopped`."""
        handle = self._open_handle(run_id)
        if node_id is not None:
            return await handle.supervisor.stop(node_id)
        return await self._end(handle, "stopped")

    async def answer(self, ask_id: str, text: str) -> AskRecord:
        for handle in self._runs.values():
            if not handle.ended and handle.supervisor.bus.has_ask(ask_id):
                return await handle.supervisor.answer_ask(ask_id, text)
        raise RuntimeControlError(f"ask {ask_id!r} is not waiting for an answer")

    async def send_to_node(
        self,
        run_id: str,
        node_id: str,
        *,
        text: str | None = None,
        data: Mapping[str, Any] | None = None,
        refs: Iterable[Ref] = (),
    ) -> NodeDelivery:
        """Hand a message to the node's current instance; refused, with its state, once it has ended."""
        handle = self._open_handle(run_id)
        record, message, queued = await handle.supervisor.deliver(node_id, text=text, data=data, refs=refs)
        return NodeDelivery(
            node_id=node_id, instance_id=record.instance_id, seq=message.seq, content_time=message.content_time, queued=queued
        )

    # -- looking --------------------------------------------------------------------

    def graph(self, run_id: str) -> Graph:
        return self._handle(run_id).graph

    async def status(self, run_id: str) -> RunStatusReport:
        handle = self._handle(run_id)
        return RunStatusReport(
            run=handle.snapshot(),
            nodes=tuple(self._node_view(handle, spec.id) for spec in handle.graph.nodes),
            pending_asks=await self.storage.pending_asks(run_id),
            graph_file=mutations.graph_file_status(handle),
        )

    async def node_detail(self, run_id: str, node_id: str) -> NodeDetail:
        handle = self._handle(run_id)
        spec = handle.graph.node(node_id)
        if spec is None:
            raise RuntimeControlError(f"no node {node_id!r} in run {run_id}")
        outputs = {
            record.port: record.message
            for record in await self.storage.latest_port_messages(run_id)
            if record.node == node_id
        }
        directory = node_dir(self.session_dir, run_id, node_id, spec.instance_id)
        return NodeDetail(
            node=self._node_view(handle, node_id),
            params=thaw(spec.params),
            code=spec.code,
            snapshot=await self.storage.node_snapshot(run_id, node_id, spec.instance_id),
            outputs=outputs,
            artifacts_dir=directory.relative_to(self.session_dir).as_posix(),
            replacements=handle.graph.replacements_of(node_id),
            instances=tuple(r for r in handle.supervisor.instance_states() if r.node_id == node_id),
            pending_asks=tuple(ask for ask in await self.storage.pending_asks(run_id) if ask.node_id == node_id),
        )

    def summary(self, run_id: str) -> str:
        """The run summary for the main agent's context."""
        return self.feedback.summary(run_id)

    def stream(self, run_id: str, modes: Iterable[str]) -> Subscription:
        """Subscribe to one run's output; iterate the result, `aclose()` it to unsubscribe."""
        self._handle(run_id)
        return self.streams.subscribe(modes, run_id=run_id)

    # -- internals --------------------------------------------------------------

    def _make_handle(self, record: RunRecord) -> RunHandle:
        supervisor = Supervisor(self.storage, self.callbacks, self.config, self.registry, policy=self.policy)
        return RunHandle(record, supervisor, self.storage, self.callbacks)

    def _handle(self, run_id: str) -> RunHandle:
        handle = self._runs.get(run_id)
        if handle is None:
            raise RuntimeControlError(f"run {run_id!r} is not open in this service")
        return handle

    def _open_handle(self, run_id: str) -> RunHandle:
        handle = self._handle(run_id)
        if handle.ended:
            raise RuntimeControlError(f"run {run_id} is {handle.status}")
        return handle

    async def _end(self, handle: RunHandle, status: Literal["finished", "stopped"]) -> Run:
        if handle.ended:
            raise RuntimeControlError(f"run {handle.run_id} is already {handle.status}")
        await handle.supervisor.shutdown()
        await handle.set_status(status)
        handle.emit(EventType.RUN_FINISHED, {"status": status, "graph_version": handle.graph.version})
        return handle.snapshot()

    def _node_view(self, handle: RunHandle, node_id: str) -> NodeView:
        spec = handle.graph.node(node_id)
        assert spec is not None
        record = handle.supervisor.state(node_id)
        entry = self.registry.find_entry(spec.type, spec.code)
        return NodeView(
            node_id=node_id,
            instance_id=spec.instance_id,
            type=spec.type,
            description=spec.description,
            state=str(record.state) if record is not None else "pending",
            outcome=record.outcome if record is not None else None,
            reason=record.reason if record is not None else "",
            error=record.error if record is not None else None,
            acts_on_environment=entry is not None and entry.acts_on_environment,
        )

    async def _values(self, run_id: str) -> dict[str, Any]:
        handle = self._runs.get(run_id)
        if handle is None:
            return {}
        graph = handle.graph
        return {
            "graph_version": graph.version,
            "graph": graph_spec_from_graph(graph).model_dump(mode="json", exclude_none=True),
            "nodes": {
                spec.id: self._node_view(handle, spec.id).model_dump(
                    include={"instance_id", "state", "outcome", "reason", "error"}
                )
                for spec in graph.nodes
            },
            "ports": {
                f"{record.node}.{record.port}": record.message.model_dump(mode="json")
                for record in await self.storage.latest_port_messages(run_id)
            },
        }
