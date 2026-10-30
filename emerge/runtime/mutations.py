"""Changing a running graph, and the graph file the main agent edits to do it.

A change arrives as a patch, from a caller or computed from the edited file, and is handed to the
supervisor whole (`Supervisor.apply_patch`): it checks the patch against the node states of that
moment and carries it out under one lock. Only what the difference names is touched. Ended nodes
stay in the graph with their outcomes, and those outcomes keep counting for start edges, also for
ones added later. Each commit overwrites the graph in Storage and is announced as `graph.patched`
with the patch and the new version, so the trace holds the whole history and Storage only the
latest graph.

The graph file (`runs/<run_id>/graph.json`, format `emerge.graph-spec/v1`) is the main agent's
draft. Nothing runs from it: editing it changes nothing until it is committed. The runtime writes it
when the run is created and after each successful commit; at any other moment it writes the file
only if the file is still exactly what it last wrote, so a draft is never overwritten.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from emerge.events import EventType
from emerge.exceptions import GraphValidationError, RuntimeControlError
from emerge.graph.graph import Graph, StartStatus, start_status
from emerge.graph.patch import GraphDiff, GraphPatch, apply_graph_patch, diff_graphs
from emerge.graph.serialization import patch_to_dict
from emerge.graph.spec import dumps_graph_spec, graph_spec_from_graph, loads_graph_spec, patch_from_graph_spec
from emerge.runtime.run import RunHandle
from emerge.runtime.state import LifecycleState
from emerge.runtime.supervisor import NodeFate, OnCommit, PatchExecution

__all__ = [
    "GraphCommitResult",
    "GraphFileStatus",
    "NodeFate",
    "apply",
    "commit_graph_file",
    "graph_file_status",
    "preview",
    "render_graph_file",
    "reset_graph_file",
    "write_graph_file",
]

CommitSource = Literal["patch", "file", "resume"]
RejectKind = Literal["format", "stale", "rejected", "missing_file"]


class GraphCommitResult(BaseModel):
    """What a commit did, or with `dry_run`, would do."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    committed: bool
    dry_run: bool = False
    # False when the patch or the file changed nothing; no version was made.
    changed: bool = False
    previous_version: int
    # The graph version after the commit; the current one when nothing was committed.
    version: int
    diff: GraphDiff = GraphDiff()
    # Every node that has not started yet after the change, and where it stands: starts now, keeps
    # waiting, can never start (recorded as skipped), or starts once its old instance has stopped.
    fates: dict[str, NodeFate] = {}
    # Replaced nodes whose old process was asked to stop and has not exited yet.
    waiting_for_old_instance: tuple[str, ...] = ()
    # Per removed node, the nodes downstream of it (subscriptions and start edges, cycles included).
    removed_downstream: dict[str, tuple[str, ...]] = {}
    # One line per change about what it reaches, for the main agent.
    impact: tuple[str, ...] = ()
    # Why nothing was committed, verbatim from the check that refused it.
    error: str | None = None
    error_kind: RejectKind | None = None

    def describe(self) -> str:
        """The result in a few lines, as the main agent reads it."""
        if self.error is not None:
            return f"not committed ({self.error_kind}): {self.error}"
        if not self.changed:
            return f"no changes; the graph stays at version {self.version}"
        head = "dry run, nothing committed" if self.dry_run else f"committed: version {self.previous_version} -> {self.version}"
        lines = [head, *self.impact]
        by_fate: dict[NodeFate, list[str]] = {}
        for node_id, fate in self.fates.items():
            by_fate.setdefault(fate, []).append(node_id)
        labels = {
            NodeFate.STARTED: "starts now" if self.dry_run else "started",
            NodeFate.WAITING: "waiting for start edges",
            NodeFate.NEVER: "can never start (skipped)",
            NodeFate.AFTER_OLD_INSTANCE: "starts once the old instance has stopped",
        }
        for fate, label in labels.items():
            if by_fate.get(fate):
                lines.append(f"{label}: {', '.join(by_fate[fate])}")
        return "\n".join(lines)


class GraphFileStatus(BaseModel):
    """The graph file against the running graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    exists: bool
    current_version: int
    # The `graph_version` written in the file; None when it states none or cannot be read.
    file_version: int | None = None
    # Differs from what the runtime last wrote: the main agent has edits that are not committed.
    has_changes: bool = False
    # Written from an older graph version: committing it would be refused.
    stale: bool = False
    # What committing the file would change, when that can be worked out.
    diff: GraphDiff | None = None
    error: str | None = None


# -- committing ---------------------------------------------------------------------------


async def apply(handle: RunHandle, patch: GraphPatch, *, source: CommitSource = "patch") -> GraphCommitResult:
    """Commit `patch` to the run; the graph file is rewritten if it holds no draft."""
    async with handle.commit_lock:
        result = await _apply(handle, patch, source)
    if result.committed:
        await write_graph_file(handle, force=False)
    return result


async def preview(handle: RunHandle, patch: GraphPatch) -> GraphCommitResult:
    """What committing `patch` now would do, without committing: nothing is stored, sent or written."""
    supervisor = handle.supervisor
    old = handle.graph
    states = {node_id: str(record.state) for node_id, record in supervisor.states().items()}
    try:
        new = apply_graph_patch(old, patch, supervisor.registry, states=states)
    except GraphValidationError as e:
        return _rejected(old, str(e), "rejected", dry_run=True)
    if new is old:
        return GraphCommitResult(committed=False, dry_run=True, previous_version=old.version, version=old.version)
    diff = diff_graphs(old, new)
    verdicts = start_status(new, supervisor.ended_outcomes())
    replaced = {record.node: record for record in diff.nodes.replaced}
    live = {LifecycleState.STARTING, LifecycleState.RUNNING, LifecycleState.PAUSED, LifecycleState.STOPPING}
    fates: dict[str, NodeFate] = {}
    for spec in new.nodes:
        record = supervisor.state(spec.id)
        not_started = spec.id in diff.nodes.added or record is None or record.state is LifecycleState.PENDING
        if spec.id in replaced and record is not None and record.state in live:
            fates[spec.id] = NodeFate.AFTER_OLD_INSTANCE
        elif spec.id in replaced or not_started:
            fates[spec.id] = _fate(verdicts.get(spec.id))
    return GraphCommitResult(
        committed=False,
        dry_run=True,
        changed=True,
        previous_version=old.version,
        version=new.version,
        diff=diff,
        fates=fates,
        removed_downstream=_removed_downstream(old, diff),
        impact=_impact(old, new, diff),
    )


def commit_recorder(handle: RunHandle, source: CommitSource) -> OnCommit:
    """What the supervisor calls once a new version has passed its checks: store it, announce it."""

    async def record(old: Graph, new: Graph, patch: GraphPatch, diff: GraphDiff) -> None:
        await handle.storage.save_graph(handle.run_id, new)
        handle.emit(
            EventType.GRAPH_PATCHED,
            {
                "version": new.version,
                "previous_version": old.version,
                "source": source,
                "patch": patch_to_dict(patch),
                "diff": diff.model_dump(mode="json"),
            },
        )

    return record


async def _apply(handle: RunHandle, patch: GraphPatch, source: CommitSource) -> GraphCommitResult:
    if handle.ended:
        raise RuntimeControlError(f"run {handle.run_id} is {handle.status}; its graph can no longer change")
    old = handle.graph
    try:
        execution = await handle.supervisor.apply_patch(patch, on_commit=commit_recorder(handle, source))
    except GraphValidationError as e:
        return _rejected(old, str(e), "rejected")
    if execution.new is old:
        return GraphCommitResult(committed=False, previous_version=old.version, version=old.version)
    await handle.refresh_status()
    return _committed(execution)


def _committed(execution: PatchExecution) -> GraphCommitResult:
    old, new, diff = execution.old, execution.new, execution.diff
    return GraphCommitResult(
        committed=True,
        changed=True,
        previous_version=old.version,
        version=new.version,
        diff=diff,
        fates=dict(execution.fates),
        waiting_for_old_instance=execution.held,
        removed_downstream=_removed_downstream(old, diff),
        impact=_impact(old, new, diff),
    )


def _rejected(graph: Graph, error: str, kind: RejectKind, *, dry_run: bool = False) -> GraphCommitResult:
    return GraphCommitResult(
        committed=False,
        dry_run=dry_run,
        previous_version=graph.version,
        version=graph.version,
        error=error,
        error_kind=kind,
    )


def _fate(verdict: StartStatus | None) -> NodeFate:
    if verdict is StartStatus.READY:
        return NodeFate.STARTED
    if verdict is StartStatus.NEVER:
        return NodeFate.NEVER
    return NodeFate.WAITING


def _removed_downstream(old: Graph, diff: GraphDiff) -> dict[str, tuple[str, ...]]:
    return {
        node_id: tuple(other for other in old.downstream_closure((node_id,)) if other != node_id)
        for node_id in diff.nodes.removed
    }


def _impact(old: Graph, new: Graph, diff: GraphDiff) -> tuple[str, ...]:
    """What each change reaches, from the graph's own change-point queries."""
    lines: list[str] = []
    for node_id in diff.nodes.added:
        affected = new.affected_by(node_id)
        waits = f"; starts after {', '.join(affected.start_dependencies)}" if affected.start_dependencies else ""
        lines.append(f"added {node_id}{waits}")
    for node_id in diff.nodes.removed:
        affected = old.affected_by(node_id)
        parts = [f"removed {node_id}"]
        readers = sorted({f"{sub.target_node}.{sub.target_port}" for sub in affected.subscriptions if sub.source_node == node_id})
        if readers:
            parts.append(f"inputs left without it: {', '.join(readers)}")
        if affected.start_dependents:
            parts.append(f"start edges naming it: {', '.join(affected.start_dependents)}")
        lines.append("; ".join(parts))
    for record in diff.nodes.replaced:
        affected = new.affected_by(record.node)
        readers = sorted({sub.target_node for sub in affected.subscriptions if sub.source_node == record.node})
        line = f"replaced {record.node}: {record.old_instance_id} -> {record.new_instance_id}"
        if readers:
            line += f"; {', '.join(readers)} read the new instance from now on"
        if affected.start_dependents:
            line += f"; start edges on it wait for the new instance: {', '.join(affected.start_dependents)}"
        lines.append(line)
    for change in diff.nodes.modified:
        lines.append(f"modified {change.node}: {', '.join(change.fields)}")
    for before, after in diff.subscriptions.rewired:
        lines.append(
            f"rewired {after.target_node}.{after.target_port}: {before.source_node}.{before.source_port} -> "
            f"{after.source_node}.{after.source_port}, read from now on"
        )
    for sub in diff.subscriptions.added:
        lines.append(f"connected {sub.source_node}.{sub.source_port} -> {sub.target_node}.{sub.target_port}")
    for sub in diff.subscriptions.removed:
        if sub.source_node not in diff.nodes.removed and sub.target_node not in diff.nodes.removed:
            lines.append(f"disconnected {sub.source_node}.{sub.source_port} -> {sub.target_node}.{sub.target_port}")
    return tuple(lines)


# -- the graph file -----------------------------------------------------------------------


def render_graph_file(graph: Graph) -> str:
    return dumps_graph_spec(graph_spec_from_graph(graph))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def holds_draft(handle: RunHandle) -> bool:
    """The file differs from what the runtime last wrote: it holds edits not committed."""
    text = _read(handle.graph_file)
    return text is not None and _digest(text) != handle.graph_file_digest


async def write_graph_file(handle: RunHandle, *, force: bool) -> bool:
    """Write the current graph to the file; without `force`, only over a file holding no draft.

    Returns whether it was written.
    """
    if not force and holds_draft(handle):
        return False
    text = render_graph_file(handle.graph)
    handle.graph_file.parent.mkdir(parents=True, exist_ok=True)
    handle.graph_file.write_text(text, encoding="utf-8")
    digest = _digest(text)
    await handle.storage.set_graph_file_digest(handle.run_id, digest)
    handle.graph_file_digest = digest
    return True


async def commit_graph_file(
    handle: RunHandle, *, replace: Iterable[str] = (), dry_run: bool = False
) -> GraphCommitResult:
    """Commit what the graph file says, through the same path as a patch.

    Refused, the file stays as the main agent left it so it can be fixed in place. Committed, the
    file is rewritten at the new version. A file written from an older version is refused rather
    than merged: judgements made against an older state do not carry over to the current one.
    """
    replace = tuple(replace)
    async with handle.commit_lock:
        graph = handle.graph
        text = _read(handle.graph_file)
        if text is None:
            return _rejected(
                graph, f"{handle.graph_file} does not exist; reset_graph_file writes the current graph", "missing_file", dry_run=dry_run
            )
        try:
            spec = loads_graph_spec(text)
        except GraphValidationError as e:
            return _rejected(graph, str(e), "format", dry_run=dry_run)
        try:
            patch = patch_from_graph_spec(graph, spec, replace=replace)
        except GraphValidationError as e:
            stale = spec.graph_version is not None and spec.graph_version != graph.version
            if stale:
                return _rejected(
                    graph, f"{e}. Call reset_graph_file, read the file again and redo the edit.", "stale", dry_run=dry_run
                )
            return _rejected(graph, str(e), "rejected", dry_run=dry_run)
        if not patch:
            return GraphCommitResult(committed=False, dry_run=dry_run, previous_version=graph.version, version=graph.version)
        if dry_run:
            return await preview(handle, patch)
        result = await _apply(handle, patch, "file")
        if result.committed:
            await write_graph_file(handle, force=True)
        return result


async def reset_graph_file(handle: RunHandle) -> None:
    """Drop whatever the file holds and write the current graph."""
    async with handle.commit_lock:
        await write_graph_file(handle, force=True)


def graph_file_status(handle: RunHandle) -> GraphFileStatus:
    graph = handle.graph
    path = handle.graph_file
    text = _read(path)
    if text is None:
        return GraphFileStatus(path=str(path), exists=False, current_version=graph.version)
    if _digest(text) == handle.graph_file_digest:
        return GraphFileStatus(path=str(path), exists=True, current_version=graph.version, file_version=graph.version)
    try:
        spec = loads_graph_spec(text)
    except GraphValidationError as e:
        return GraphFileStatus(path=str(path), exists=True, current_version=graph.version, has_changes=True, error=str(e))
    stale = spec.graph_version is not None and spec.graph_version != graph.version
    diff: GraphDiff | None = None
    error: str | None = None
    try:
        # Compared with the current graph whatever version it was written from, structurally only:
        # whether the node states allow it is decided by the commit.
        patch = patch_from_graph_spec(graph, spec.model_copy(update={"graph_version": None}))
        diff = diff_graphs(graph, apply_graph_patch(graph, patch, handle.supervisor.registry))
    except GraphValidationError as e:
        error = str(e)
    return GraphFileStatus(
        path=str(path),
        exists=True,
        current_version=graph.version,
        file_version=spec.graph_version,
        has_changes=diff is None or bool(diff) or stale,
        stale=stale,
        diff=diff,
        error=error,
    )
