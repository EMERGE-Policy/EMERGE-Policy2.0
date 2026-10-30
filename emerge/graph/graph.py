"""The graph: node instances, subscriptions, start edges, and queries over them.

A graph is an immutable value. Changes go through `patch.py`, which produces a new version and
leaves the old object untouched. Queries are structural only: who can produce for whom, what
starts after what, and which parts of the graph a change would reach.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from emerge.graph.edge import StartEdge, Subscription
from emerge.graph.ports import NAME_PATTERN, FrozenDict, FrozenJson

Direction = Literal["upstream", "downstream"]


class StartStatus(StrEnum):
    """What `start_status` finds for a node that has not ended: start now, keep waiting, or never."""

    READY = "ready"
    WAITING = "waiting"
    NEVER = "never"


def make_instance_id(node_id: str, version: int) -> str:
    """Id of the instance that graph `version` created for `node_id`.

    Versions only grow within a run, so an id is never handed out twice: a node removed and added
    again, or replaced, always gets an instance its predecessors never had.
    """
    return f"{node_id}@v{version}"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeSpec(_Frozen):
    """One node instance in a graph. `type` is a registered name; the class lives in registry.py."""

    id: str = Field(pattern=NAME_PATTERN)
    type: str = Field(min_length=1)
    params: FrozenJson = Field(default_factory=FrozenDict)
    # Start conditions: the node starts once every source on any one of these edges has finished as
    # required. Empty means it is a root and starts as soon as it joins the graph.
    start_edges: tuple[StartEdge, ...] = ()
    # What this instance is for, in one line from the main agent; `Node.description` says what the
    # type does.
    description: str = ""
    # The incarnation behind the logical id. A replacement keeps `id`, so subscriptions and start
    # edges stay put, and gets a new instance, so the runtime can tell the old process's late
    # messages and exit from the new one's.
    instance_id: str = Field(min_length=1)
    # Implementation version of a generated node type, fixed when the instance is placed; None for
    # types from configured modules, which have one implementation per process.
    code: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _first_instance(cls, data: Any) -> Any:
        # Specs written by hand or by the builder describe a graph's first version.
        if isinstance(data, dict) and not data.get("instance_id") and isinstance(data.get("id"), str):
            return {**data, "instance_id": make_instance_id(data["id"], 0)}
        return data


class Replacement(_Frozen):
    """One instance of a logical node giving way to the next, committed at graph `version`."""

    node: str
    version: int = Field(ge=1)
    old_instance_id: str
    new_instance_id: str
    old_type: str
    new_type: str
    old_code: str | None = None
    new_code: str | None = None


@dataclass(frozen=True, slots=True)
class Affected:
    """What a change to one node or one of its ports reaches."""

    subscriptions: tuple[Subscription, ...] = ()
    # Nodes this one's start edges wait on, and nodes waiting on this one.
    start_dependencies: tuple[str, ...] = ()
    start_dependents: tuple[str, ...] = ()
    reads: tuple[tuple[str, str], ...] = ()


class Graph(_Frozen):
    version: int = Field(default=0, ge=0)
    nodes: tuple[NodeSpec, ...] = ()
    subscriptions: tuple[Subscription, ...] = ()
    # Every replacement since the first version, oldest first, kept after the node is removed.
    replacements: tuple[Replacement, ...] = ()

    # -- lookup -------------------------------------------------------------

    def node(self, node_id: str) -> NodeSpec | None:
        # A linear scan, not a cached index: model_copy(update=...) copies cached state along with
        # the fields and would leave it stale, and a graph holds tens of nodes. With duplicate ids
        # the first wins; validation reports the rest as DUPLICATE_ID.
        return next((node for node in self.nodes if node.id == node_id), None)

    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    # -- instances ----------------------------------------------------------

    def is_current(self, node_id: str, instance_id: str) -> bool:
        """Whether `instance_id` is the instance this version runs for `node_id`.

        The runtime asks this before a message, an exit or any other event from a node process
        may count: a replaced instance can still be shutting down and must not be heard.
        """
        node = self.node(node_id)
        return node is not None and node.instance_id == instance_id

    def replacements_of(self, node_id: str) -> tuple[Replacement, ...]:
        return tuple(record for record in self.replacements if record.node == node_id)

    # -- subscriptions ------------------------------------------------------

    def source_of(self, node_id: str, port: str) -> Subscription | None:
        """The single subscription feeding an input port, if any."""
        wanted = (node_id, port)
        for sub in self.subscriptions:
            if (sub.target_node, sub.target_port) == wanted:
                return sub
        return None

    def consumers_of(self, node_id: str, port: str) -> tuple[Subscription, ...]:
        return tuple(
            sub for sub in self.subscriptions if (sub.source_node, sub.source_port) == (node_id, port)
        )

    def upstream(self, node_id: str) -> tuple[str, ...]:
        """Nodes whose outputs feed this node's inputs."""
        return _unique(sub.source_node for sub in self.subscriptions if sub.target_node == node_id)

    def downstream(self, node_id: str) -> tuple[str, ...]:
        """Nodes whose inputs read this node's outputs."""
        return _unique(sub.target_node for sub in self.subscriptions if sub.source_node == node_id)

    # -- start edges --------------------------------------------------------

    def start_edges_of(self, node_id: str) -> tuple[StartEdge, ...]:
        node = self.node(node_id)
        return node.start_edges if node else ()

    def start_dependencies(self, node_id: str) -> tuple[str, ...]:
        """Every node a start edge of this node waits on, in declaration order, once each."""
        return _unique(source.node for edge in self.start_edges_of(node_id) for source in edge.sources)

    def start_dependents(self, node_id: str) -> tuple[str, ...]:
        """Nodes having a start edge with `node_id` among its sources."""
        return _unique(
            node.id
            for node in self.nodes
            if any(source.node == node_id for edge in node.start_edges for source in edge.sources)
        )

    def node_ids_without_start_dependencies(self) -> tuple[str, ...]:
        """Nodes with no start condition; they start as soon as they join a running graph."""
        return tuple(node.id for node in self.nodes if not node.start_edges)

    # -- traversal ----------------------------------------------------------

    def upstream_closure(
        self, node_ids: Iterable[str], *, include_start_edges: bool = True
    ) -> tuple[str, ...]:
        """Every node this one depends on, directly or through other nodes."""
        return self._closure(node_ids, "upstream", include_start_edges)

    def downstream_closure(
        self, node_ids: Iterable[str], *, include_start_edges: bool = True
    ) -> tuple[str, ...]:
        """Every node that depends on this one, directly or through other nodes."""
        return self._closure(node_ids, "downstream", include_start_edges)

    def _closure(
        self, node_ids: Iterable[str], direction: Direction, include_start_edges: bool
    ) -> tuple[str, ...]:
        # Subscriptions may form cycles, so walk iteratively with a `seen` set: the walk always
        # terminates and lists each reachable node exactly once.
        known = set(self.node_ids())
        seen: set[str] = {node_id for node_id in node_ids if node_id in known}
        queue = deque(seen)
        while queue:
            current = queue.popleft()
            if direction == "upstream":
                neighbours = self.upstream(current)
                if include_start_edges:
                    neighbours += self.start_dependencies(current)
            else:
                neighbours = self.downstream(current)
                if include_start_edges:
                    neighbours += self.start_dependents(current)
            for neighbour in neighbours:
                # A start edge may name an id that is not in the graph; that is
                # START_UNKNOWN_NODE, not a node.
                if neighbour in known and neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        return tuple(sorted(seen))

    # -- change impact ------------------------------------------------------

    def affected_by(
        self,
        node_id: str,
        port: str | None = None,
        *,
        reads_by_type: Mapping[str, Sequence[str]] | None = None,
    ) -> Affected:
        """Everything touching a node, or one of its ports.

        Reports the subscriptions, the start edges and the external live sources a change would
        disturb, so the caller can decide what to stop or rewire before committing it.
        """
        subscriptions = tuple(
            sub
            for sub in self.subscriptions
            if (sub.source_node == node_id and (port is None or sub.source_port == port))
            or (sub.target_node == node_id and (port is None or sub.target_port == port))
        )
        reads = self.external_reads(reads_by_type, (node_id,)) if reads_by_type else ()
        return Affected(
            subscriptions=subscriptions,
            start_dependencies=self.start_dependencies(node_id),
            start_dependents=self.start_dependents(node_id),
            reads=reads,
        )

    def external_reads(
        self,
        reads_by_type: Mapping[str, Sequence[str]],
        node_ids: Iterable[str] = (),
    ) -> tuple[tuple[str, str], ...]:
        """(node id, source name) for nodes reading live sources directly, per registry metadata."""
        wanted = set(node_ids) or None
        return tuple(
            (node.id, source)
            for node in self.nodes
            if (wanted is None or node.id in wanted)
            for source in reads_by_type.get(node.type, ())
        )

    # -- start-edge cycles --------------------------------------------------

    def start_edge_cycles(self) -> list[tuple[str, ...]]:
        """One closed path per cycle among start edges; empty when they are acyclic.

        Distinct loops sharing a node are reported once each; the path starts and ends on the node
        where the walk first re-entered the loop. A node that waits on itself gives ("x", "x").
        """
        remaining = self._start_edge_residue()
        cycles: list[tuple[str, ...]] = []
        reported: set[frozenset[str]] = set()
        for start in sorted(remaining):
            path: list[str] = []
            seen_at: dict[str, int] = {}
            current = start
            while current not in seen_at:
                seen_at[current] = len(path)
                path.append(current)
                # Every residue node has a source that is also in the residue.
                current = next(dep for dep in self.start_dependencies(current) if dep in remaining)
            loop = frozenset(path[seen_at[current] :])
            if loop not in reported:
                reported.add(loop)
                cycles.append((*path[seen_at[current] :], current))
        return cycles

    def _start_edge_residue(self) -> set[str]:
        """Nodes on or behind a cycle among start edges, found by peeling off acyclic ones."""
        known = set(self.node_ids())
        pending = {
            node.id: len(set(self.start_dependencies(node.id)) & known)
            for node in self.nodes
        }
        ready = deque(node_id for node_id, count in pending.items() if count == 0)
        while ready:
            done = ready.popleft()
            for dependent in self.start_dependents(done):
                if dependent in pending:
                    pending[dependent] -= 1
                    if pending[dependent] == 0:
                        ready.append(dependent)
        return {node_id for node_id, count in pending.items() if count > 0}

    # -- display ------------------------------------------------------------

    def __str__(self) -> str:
        lines = [f"graph v{self.version}: {len(self.nodes)} nodes, {len(self.subscriptions)} subscriptions"]
        for node in self.nodes:
            lines.append(f"  {node.id} : {node.type}")
        for sub in self.subscriptions:
            lines.append(f"  {sub.source_node}.{sub.source_port} -> {sub.target_node}.{sub.target_port}")
        for node in self.nodes:
            for edge in node.start_edges:
                lines.append(f"  {edge} ..> {node.id} (start)")
        for record in self.replacements:
            lines.append(f"  {record.old_instance_id} => {record.new_instance_id} (replaced at v{record.version})")
        return "\n".join(lines)


def start_status(graph: Graph, ended: Mapping[str, str | None]) -> dict[str, StartStatus]:
    """Whether each node that has not ended can start now, has to wait, or can never start.

    `ended` maps every node that is over to the outcome it gave, or None when it ended without one.
    Skipped nodes are left out: finding them is what this function does (listing one as None
    changes nothing for the others). Both the supervisor and the patch check call this, so the rule
    exists once. A node that is already running comes back READY too; only pending ones act on it.

    An edge fires once every source on it ended with the outcome it asks for, any outcome for
    "done". It can never fire once a source ended otherwise or without an outcome, can never start
    itself, or is not in the graph. A node is ready when it has no start edge or one of them fired,
    and can never start when none of its edges can fire any more; that passes on down the edges.
    """
    edges: dict[str, tuple[StartEdge, ...]] = {}
    for node in graph.nodes:
        # With duplicate ids the first wins, as in Graph.node(); validation reports the rest.
        edges.setdefault(node.id, node.start_edges)
    open_ids = [node_id for node_id in edges if node_id not in ended]
    # Grown outwards from the roots and the outcomes so far, so a node behind a source that can never
    # start is never reached, and neither is one on a cycle, which validation rejects anyway.
    possible: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node_id in open_ids:
            if node_id not in possible and (
                not edges[node_id] or any(_edge_can_fire(edge, ended, possible) for edge in edges[node_id])
            ):
                possible.add(node_id)
                changed = True
    result: dict[str, StartStatus] = {}
    for node_id in open_ids:
        if not edges[node_id] or any(_edge_has_fired(edge, ended) for edge in edges[node_id]):
            result[node_id] = StartStatus.READY
        elif node_id in possible:
            result[node_id] = StartStatus.WAITING
        else:
            result[node_id] = StartStatus.NEVER
    return result


def _edge_has_fired(edge: StartEdge, ended: Mapping[str, str | None]) -> bool:
    # An edge with no sources is a malformed graph; it fires nothing rather than firing at once.
    return bool(edge.sources) and all(
        source.node in ended and _matches(ended[source.node], source.outcome) for source in edge.sources
    )


def _edge_can_fire(edge: StartEdge, ended: Mapping[str, str | None], possible: set[str]) -> bool:
    # Each source has met its requirement already, or has not ended and can still start.
    return bool(edge.sources) and all(
        _matches(ended[source.node], source.outcome) if source.node in ended else source.node in possible
        for source in edge.sources
    )


def _matches(outcome: str | None, required: str | None) -> bool:
    # None asks for "done", and a node that ended without an outcome is not done in any sense.
    return outcome is not None and (required is None or outcome == required)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
