"""Closing a run with its session, and carrying it on when the session is opened again.

That is the one thing this module is for: the user leaves in the middle of a run and later opens
the session to go on. No history is kept, nothing is rolled back or replayed. What a reopened run
needs was written to Storage as it happened (node states and outcomes, port messages, asks, queued
messages, the timeline number, the latest graph); closing only stops the processes, and reopening
reads it back and goes on from there, as LangGraph reuses the writes of nodes that already
succeeded instead of running them again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from emerge.exceptions import CheckpointError, GraphValidationError
from emerge.graph.graph import Graph
from emerge.graph.registry import NodeRegistry
from emerge.graph.spec import loads_graph_spec
from emerge.runtime import mutations
from emerge.runtime.run import RunHandle
from emerge.runtime.state import LifecycleState
from emerge.runtime.supervisor import NodeFate
from emerge.storage.artifacts import code_path, ref_dir
from emerge.storage.base import AskRecord, PortRecord, RunRecord, RunStatus


class RerunEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    old_instance_id: str
    new_instance_id: str
    # What the old instance was doing when the session closed.
    was: LifecycleState
    # Started paused: it was paused, or the whole run was.
    paused: bool


class UnavailableRef(BaseModel):
    """A reference in a kept port message whose files are no longer in the workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    port: str
    version: int
    producer: str
    producer_instance: str
    key: str
    path: str


class ResumeReport(BaseModel):
    """What reopening did to each node, for the main agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: RunStatus
    graph_version: int
    # Ended before the session closed: left as they were, outcomes still counting.
    kept: tuple[str, ...] = ()
    # Were running: each runs again from the start as a new instance; the old one is recorded stopped.
    rerun: tuple[RerunEntry, ...] = ()
    # Were stopping: recorded as stopped, not started again.
    stopped: tuple[str, ...] = ()
    # Had not started: started now, still waiting, or can never start (skipped).
    started: tuple[str, ...] = ()
    waiting: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    # Questions the old instances left unanswered; void now. A new instance asks again if it needs to.
    voided_asks: tuple[AskRecord, ...] = ()
    # Main-agent messages still waiting for nodes that have not started; they get them when they do.
    queued_messages: int = 0
    # The graph file held edits not committed before the session closed; it was left as it was.
    draft_kept: bool = False
    # That draft was written from an older graph version than the current one.
    draft_stale: bool = False
    unavailable_refs: tuple[UnavailableRef, ...] = ()

    def describe(self) -> str:
        lines = [f"run {self.run_id} reopened at graph version {self.graph_version}, now {self.status}"]
        if self.kept:
            lines.append(f"ended before, kept as they were: {', '.join(self.kept)}")
        for entry in self.rerun:
            paused = ", paused" if entry.paused else ""
            lines.append(f"was {entry.was}, runs again from the start: {entry.node_id} ({entry.new_instance_id}{paused})")
        if self.stopped:
            lines.append(f"were stopping, now stopped: {', '.join(self.stopped)}")
        if self.started:
            lines.append(f"start edges met, started: {', '.join(self.started)}")
        if self.waiting:
            lines.append(f"still waiting: {', '.join(self.waiting)}")
        if self.never:
            lines.append(f"can never start: {', '.join(self.never)}")
        for ask in self.voided_asks:
            lines.append(f"question {ask.id} from {ask.node_id} is void: {ask.question}")
        if self.queued_messages:
            lines.append(f"{self.queued_messages} queued message(s) wait for nodes that have not started")
        if self.draft_kept:
            stale = ", written from an older graph version (reset_graph_file before editing)" if self.draft_stale else ""
            lines.append(f"graph.json still holds the draft not committed before closing{stale}")
        for ref in self.unavailable_refs:
            lines.append(f"unavailable: files of {ref.producer}.{ref.port}/{ref.key} ({ref.producer_instance}) are gone")
        return "\n".join(lines)


async def close(handle: RunHandle) -> None:
    """The session is closing: stop every node process, keep every node's state as it is."""
    await handle.supervisor.shutdown(closing=True)
    await handle.set_status("closed")


async def resume(
    record: RunRecord,
    make_handle: Callable[[RunRecord], RunHandle],
    registry: NodeRegistry,
) -> tuple[RunHandle, ResumeReport]:
    """Carry on a run closed with its session."""
    if record.status != "closed":
        raise CheckpointError(f"run {record.id} is {record.status}; only a run closed with its session is continued")
    handle = make_handle(record)
    storage = handle.storage
    state = await storage.load_run_state(record.id)
    assert state is not None
    graph = state.graph if state.graph is not None else Graph()
    load_pinned_code(registry, handle.workspace, graph)
    # Their instances are gone; a new instance that needs the answer asks again.
    voided = tuple([await storage.void_ask(ask.id) for ask in state.pending_asks])
    execution = await handle.supervisor.continue_run(
        graph,
        record.id,
        state.node_states,
        paused=record.paused,
        on_commit=mutations.commit_recorder(handle, "resume"),
    )
    written = await mutations.write_graph_file(handle, force=False)
    draft_stale = False
    if not written:
        try:
            draft_stale = loads_graph_spec(handle.graph_file.read_text(encoding="utf-8")).graph_version not in (
                None,
                handle.graph.version,
            )
        except GraphValidationError:
            draft_stale = False
    await handle.reopen()
    fates = execution.fates
    report = ResumeReport(
        run_id=record.id,
        status=handle.status,
        graph_version=handle.graph.version,
        kept=execution.kept,
        rerun=tuple(
            RerunEntry(
                node_id=entry.node_id,
                old_instance_id=entry.old_instance_id,
                new_instance_id=entry.new_instance_id,
                was=entry.was,
                paused=entry.started_paused,
            )
            for entry in execution.rerun
        ),
        stopped=execution.stopped,
        started=tuple(node_id for node_id, fate in fates.items() if fate is NodeFate.STARTED),
        waiting=tuple(node_id for node_id, fate in fates.items() if fate is NodeFate.WAITING),
        never=tuple(node_id for node_id, fate in fates.items() if fate is NodeFate.NEVER),
        voided_asks=voided,
        queued_messages=len(state.queued_messages),
        draft_kept=not written,
        draft_stale=draft_stale,
        unavailable_refs=_unavailable_refs(handle.workspace, record.id, state.port_messages),
    )
    return handle, report


def load_pinned_code(registry: NodeRegistry, workspace: Path, graph: Graph) -> None:
    """Register the generated code versions the graph's nodes are pinned to, from `code/<name>/`.

    The catalog keeps showing what it showed before: an older version loaded here for a pinned
    instance does not become the current implementation of its name.
    """
    for spec in graph.nodes:
        if spec.code is None or registry.find_entry(spec.type, spec.code) is not None:
            continue
        current = registry.find_entry(spec.type)
        registry.register_from_file(code_path(workspace, spec.type, spec.code), spec.code)
        if current is not None and current.locator.origin == "file" and current.locator.path and current.locator.version:
            registry.register_from_file(current.locator.path, current.locator.version)


def _unavailable_refs(workspace: Path, run_id: str, records: Iterable[PortRecord]) -> tuple[UnavailableRef, ...]:
    # Stated as they are: a reference whose files are gone is reported, never pointed elsewhere.
    missing: list[UnavailableRef] = []
    for record in records:
        for ref in record.message.refs:
            path = ref_dir(workspace, run_id, ref)
            if not path.exists():
                missing.append(
                    UnavailableRef(
                        node=record.node,
                        port=record.port,
                        version=record.version,
                        producer=ref.producer,
                        producer_instance=ref.producer_instance,
                        key=ref.key,
                        path=str(path),
                    )
                )
    return tuple(missing)
