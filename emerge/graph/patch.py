"""Graph edits as data: compute, validate, commit.

A patch never touches the graph it is applied to. It computes a candidate structure, validates it,
and only then produces a new version; if validation fails, the old graph is returned unchanged and
nothing is committed. Whether and when a change may take effect during a run is a runtime question
and is answered elsewhere.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from emerge.exceptions import GraphValidationError
from emerge.graph.edge import StartEdge, Subscription, parse_endpoint, start_edges_from_after
from emerge.graph.graph import Graph, NodeSpec, Replacement, make_instance_id
from emerge.graph.ports import FrozenDict, FrozenJson
from emerge.graph.registry import NodeRegistry, get_active_registry
from emerge.graph.validation import Issue, IssueCode, validate

Op = Literal[
    "add_node", "remove_node", "update_params", "set_description", "connect", "disconnect", "set_start_edges", "replace_node"
]

# Lifecycle state names from runtime/state.py. A node `states` leaves out has not started: pending.
# A node may be replaced while it has not started or is still running. `stopping` is excluded
# because the process is already ending.
REPLACEABLE_STATES = frozenset({"pending", "starting", "running", "paused"})
# Ended for good. The node stays in the graph exactly as it ended: type, params, description, start
# edges and inputs are all final. Its outputs stay readable, so new nodes may subscribe to them.
# `skipped` is a node whose start edges can never fire.
TERMINAL_STATES = frozenset({"finished", "failed", "stopped", "skipped"})

logger = logging.getLogger(__name__)


class Edit(BaseModel):
    """One structural change. Which fields are read depends on `op`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Op
    node: str | None = None
    type: str | None = None
    # None means "keep" for replace_node and "no parameters" elsewhere.
    params: FrozenJson | None = None
    start_edges: tuple[StartEdge, ...] = ()
    # None means "keep" for replace_node and "none" for add_node.
    description: str | None = None
    code: str | None = None
    source: str | None = None
    target: str | None = None


class GraphPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    edits: tuple[Edit, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.edits)

    @classmethod
    def add_node(
        cls,
        node_id: str,
        node_type: str,
        *,
        params: dict[str, Any] | None = None,
        start_edges: tuple[StartEdge, ...] = (),
        after: tuple[str, ...] = (),
        description: str = "",
        code: str | None = None,
    ) -> GraphPatch:
        """Place a node. `after` is shorthand for one more start edge on which each node has to be done."""
        edit = Edit(
            op="add_node",
            node=node_id,
            type=node_type,
            params=params or {},
            start_edges=(*start_edges, *start_edges_from_after(after)),
            description=description,
            code=code,
        )
        return cls(edits=(edit,))

    @classmethod
    def remove_node(cls, node_id: str) -> GraphPatch:
        return cls(edits=(Edit(op="remove_node", node=node_id),))

    @classmethod
    def replace_node(
        cls,
        node_id: str,
        node_type: str | None = None,
        *,
        params: dict[str, Any] | None = None,
        description: str | None = None,
        code: str | None = None,
    ) -> GraphPatch:
        """Put a new instance behind `node_id`, keeping its subscriptions and start edges.

        Omitted `node_type`, `params` and `description` keep the current ones; the main agent's tool
        always writes a new description, so keeping it is only the fallback. `code` pins a generated
        type's implementation version; omitted, it pins the version registered when the patch is
        applied.
        """
        edit = Edit(op="replace_node", node=node_id, type=node_type, params=params, description=description, code=code)
        return cls(edits=(edit,))

    @classmethod
    def replace_node_params(cls, node_id: str, params: dict[str, Any]) -> GraphPatch:
        return cls(edits=(Edit(op="update_params", node=node_id, params=params),))

    @classmethod
    def set_node_description(cls, node_id: str, description: str) -> GraphPatch:
        return cls(edits=(Edit(op="set_description", node=node_id, description=description),))

    @classmethod
    def connect(cls, source: str, target: str) -> GraphPatch:
        return cls(edits=(Edit(op="connect", source=source, target=target),))

    @classmethod
    def disconnect(cls, source: str, target: str) -> GraphPatch:
        return cls(edits=(Edit(op="disconnect", source=source, target=target),))

    @classmethod
    def replace_start_edges(
        cls, node_id: str, start_edges: tuple[StartEdge, ...] = (), *, after: tuple[str, ...] = ()
    ) -> GraphPatch:
        """Set all of a pending node's start edges at once; none makes it a root."""
        edges = (*start_edges, *start_edges_from_after(after))
        return cls(edits=(Edit(op="set_start_edges", node=node_id, start_edges=edges),))

    def combine_patches(self, *others: GraphPatch) -> GraphPatch:
        return GraphPatch(edits=(*self.edits, *(edit for patch in others for edit in patch.edits)))


def apply_graph_patch(
    graph: Graph,
    patch: GraphPatch,
    registry: NodeRegistry | None = None,
    *,
    states: Mapping[str, str] | None = None,
) -> Graph:
    """Return the patched, validated graph at the next version.

    `states` maps node ids to their lifecycle state in a running graph; nodes it leaves out count
    as not started. Raises GraphValidationError and leaves `graph` untouched when an edit cannot be
    made or the result has an error.
    """
    # An empty patch changes nothing; committing it would only add a version to the history.
    if not patch.edits:
        return graph
    draft = _Draft(graph, registry if registry is not None else get_active_registry(), states or {})
    for edit in patch.edits:
        draft.apply(edit)
    candidate = Graph(
        version=draft.version,
        nodes=tuple(draft.nodes.values()),
        subscriptions=tuple(draft.subscriptions),
        replacements=(*graph.replacements, *draft.replacements),
    )
    result = validate(candidate, draft.registry)
    errors = [*result.errors, *_replacement_ports(graph, candidate, draft.replacements, draft.registry)]
    if errors:
        detail = "; ".join(f"{issue.code}: {issue.message}" for issue in errors)
        raise GraphValidationError(f"patch rejected at version {candidate.version}: {detail}")
    for warning in result.warnings:
        logger.warning("graph warning after patch v%s: %s: %s", candidate.version, warning.code, warning.message)
    return candidate


class _Draft:
    """The structure a patch is building, edit by edit, before it is validated."""

    def __init__(self, graph: Graph, registry: NodeRegistry, states: Mapping[str, str]) -> None:
        self.version = graph.version + 1
        self.registry = registry
        self.states = states
        self.nodes = {node.id: node for node in graph.nodes}
        self.subscriptions = list(graph.subscriptions)
        self.replacements: list[Replacement] = []
        self.removed: set[str] = set()
        # Ids that already got a new instance in this patch. Instance ids derive from the version,
        # so a second one in the same patch would repeat the first.
        self.fresh: set[str] = set()

    def apply(self, edit: Edit) -> None:
        match edit.op:
            case "add_node":
                node_id, node_type = _need(edit.node, "node"), _need(edit.type, "type")
                if node_id in self.nodes:
                    raise GraphValidationError(f"node {node_id!r} already exists")
                if node_id in self.removed:
                    # Removing and re-adding in one patch would replace the node without the state
                    # check or the history record.
                    raise GraphValidationError(
                        f"node {node_id!r} is removed by this patch; use replace_node to put a new instance behind it"
                    )
                self._claim_instance(node_id)
                self.nodes[node_id] = NodeSpec(
                    id=node_id,
                    type=node_type,
                    params=edit.params or {},
                    start_edges=edit.start_edges,
                    description=edit.description or "",
                    instance_id=make_instance_id(node_id, self.version),
                    code=self.registry.pin(node_type, edit.code),
                )
            case "remove_node":
                node_id = self._existing(edit)
                state = self._state(node_id)
                if state != "pending":
                    then = "it stays in the graph as it ended" if state in TERMINAL_STATES else "stop it instead; it then stays in the graph"
                    raise GraphValidationError(
                        f"node {node_id!r} is {state}: only a node that has not started can be removed; {then}"
                    )
                if any(record.node == node_id for record in self.replacements):
                    raise GraphValidationError(f"node {node_id!r} is replaced by this patch and cannot also be removed")
                # A node past pending keeps its start edges as they are, so nothing could repair an
                # edge left naming a removed node; refuse here with the reason instead of letting
                # validation report an unknown node the caller cannot fix.
                fixed = [
                    other.id
                    for other in self.nodes.values()
                    if self._state(other.id) != "pending"
                    and any(source.node == node_id for edge in other.start_edges for source in edge.sources)
                ]
                if fixed:
                    raise GraphValidationError(
                        f"node {node_id!r} is named in the start edges of {', '.join(map(repr, fixed))}, which "
                        "are no longer pending and cannot change, so it cannot be removed"
                    )
                del self.nodes[node_id]
                self.removed.add(node_id)
                # Its subscriptions go with it, even into an ended node: a node that never started
                # sent nothing along them, so the ended node's record loses nothing real. Start edges of pending nodes do not: clearing one would
                # let a node start on its own, so they are left in place and validation rejects the
                # patch until the caller states what those nodes wait for.
                self.subscriptions[:] = [
                    sub for sub in self.subscriptions if node_id not in (sub.source_node, sub.target_node)
                ]
            case "replace_node":
                self._replace(edit)
            case "update_params":
                node_id = self._existing(edit)
                self._refuse_ended(node_id, "its parameters cannot change")
                params = edit.params if edit.params is not None else FrozenDict()
                if self._state(node_id) == "pending":
                    self.nodes[node_id] = self.nodes[node_id].model_copy(update={"params": params})
                else:
                    # A process already runs with the old params. New ones take a new process, and a
                    # new process is a new instance, so nothing late from the old one counts as the
                    # new one's. The code version stays: only the params were asked to change.
                    self._replace(Edit(op="replace_node", node=node_id, params=params, code=self.nodes[node_id].code))
            case "set_description":
                node_id = self._existing(edit)
                self._refuse_ended(node_id, "its description cannot change")
                self.nodes[node_id] = self.nodes[node_id].model_copy(update={"description": edit.description or ""})
            case "connect":
                sub = _subscription(edit)
                if sub not in self.subscriptions:
                    self._refuse_ended(sub.target_node, "its inputs take no new subscriptions")
                    self.subscriptions.append(sub)
            case "disconnect":
                sub = _subscription(edit)
                if sub not in self.subscriptions:
                    raise GraphValidationError(
                        f"no subscription {sub.source_node}.{sub.source_port} -> {sub.target_node}.{sub.target_port}"
                    )
                self._refuse_ended(sub.target_node, "its inputs are part of what it ended with")
                self.subscriptions.remove(sub)
            case "set_start_edges":
                node_id = self._existing(edit)
                state = self._state(node_id)
                if state != "pending":
                    raise GraphValidationError(
                        f"node {node_id!r} is {state}: start edges can only change before the node starts"
                    )
                self.nodes[node_id] = self.nodes[node_id].model_copy(update={"start_edges": edit.start_edges})

    def _replace(self, edit: Edit) -> None:
        node_id = self._existing(edit)
        state = self._state(node_id)
        if state not in REPLACEABLE_STATES:
            raise GraphValidationError(
                f"node {node_id!r} is {state}: only a node that has not started or is still running can be "
                "replaced or given new parameters"
            )
        self._claim_instance(node_id)
        current = self.nodes[node_id]
        node_type = edit.type or current.type
        # Subscriptions and start edges name the logical id, so they carry over untouched.
        new = NodeSpec(
            id=node_id,
            type=node_type,
            params=current.params if edit.params is None else edit.params,
            start_edges=current.start_edges,
            description=current.description if edit.description is None else edit.description,
            instance_id=make_instance_id(node_id, self.version),
            code=self.registry.pin(node_type, edit.code),
        )
        self.nodes[node_id] = new
        self.replacements.append(
            Replacement(
                node=node_id,
                version=self.version,
                old_instance_id=current.instance_id,
                new_instance_id=new.instance_id,
                old_type=current.type,
                new_type=new.type,
                old_code=current.code,
                new_code=new.code,
            )
        )

    def _state(self, node_id: str) -> str:
        return self.states.get(node_id, "pending")

    def _refuse_ended(self, node_id: str, consequence: str) -> None:
        state = self._state(node_id)
        if state in TERMINAL_STATES:
            raise GraphValidationError(f"node {node_id!r} is {state}: an ended node stays as it ended, so {consequence}")

    def _existing(self, edit: Edit) -> str:
        node_id = _need(edit.node, "node")
        if node_id not in self.nodes:
            raise GraphValidationError(f"node {node_id!r} is not in the graph")
        return node_id

    def _claim_instance(self, node_id: str) -> None:
        if node_id in self.fresh:
            raise GraphValidationError(f"node {node_id!r} already gets a new instance in this patch")
        self.fresh.add(node_id)


def _replacement_ports(
    old: Graph, new: Graph, records: list[Replacement], registry: NodeRegistry
) -> list[Issue]:
    """Kept subscriptions whose port type tags matched before a replacement and no longer do.

    A missing port is already ENDPOINT_MISSING. A tag mismatch is only a warning elsewhere, but a
    replacement that silently breaks wiring that used to fit is refused: the caller has to rewire
    in the same patch.
    """
    replaced = {record.node for record in records}
    issues: list[Issue] = []
    for sub in new.subscriptions:
        if sub not in old.subscriptions or not replaced & {sub.source_node, sub.target_node}:
            continue
        before = _tags(old, sub, registry)
        after = _tags(new, sub, registry)
        if before is not None and after is not None and before[0] == before[1] and after[0] != after[1]:
            issues.append(
                Issue(
                    code=IssueCode.REPLACEMENT_PORT_MISMATCH,
                    message=(
                        f"{sub.source_node}.{sub.source_port} -> {sub.target_node}.{sub.target_port} carried "
                        f"{before[0]!r}, but after the replacement it connects {after[0]!r} to {after[1]!r}; "
                        "rewire it in the same patch"
                    ),
                    subscription=sub,
                )
            )
    return issues


def _tags(graph: Graph, sub: Subscription, registry: NodeRegistry) -> tuple[str, str] | None:
    source = _port_tag(graph, sub.source_node, sub.source_port, registry, output=True)
    target = _port_tag(graph, sub.target_node, sub.target_port, registry, output=False)
    return None if source is None or target is None else (source, target)


def _port_tag(graph: Graph, node_id: str, port: str, registry: NodeRegistry, *, output: bool) -> str | None:
    node = graph.node(node_id)
    entry = registry.find_entry(node.type, node.code) if node is not None else None
    if entry is None:
        return None
    ports = entry.outputs if output else entry.inputs
    return next((p.type_tag for p in ports if p.name == port), None)


def _subscription(edit: Edit) -> Subscription:
    source_node, source_port = parse_endpoint(_need(edit.source, "source"))
    target_node, target_port = parse_endpoint(_need(edit.target, "target"))
    return Subscription(
        source_node=source_node, source_port=source_port, target_node=target_node, target_port=target_port
    )


def _need(value: str | None, field_name: str) -> str:
    if value is None:
        raise GraphValidationError(f"patch edit is missing {field_name!r}")
    return value


class NodeModification(BaseModel):
    """A node that kept its instance while some of its fields changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    # NodeSpec field names in declaration order, e.g. ("params",) or ("start_edges", "description").
    # The runtime acts on them differently: new params need a new process, new start edges are
    # checked against the outcomes so far, a new description is only shown.
    fields: tuple[str, ...]


class NodeChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    # Same instance, other fields changed.
    modified: tuple[NodeModification, ...] = ()
    # Same id, new instance: the runtime stops the old process and starts a new one. A node removed
    # and added again under its id lands here too, without a history record.
    replaced: tuple[Replacement, ...] = ()


class SubscriptionChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    added: tuple[Subscription, ...] = ()
    removed: tuple[Subscription, ...] = ()
    # An input port whose source changed: (old, new). One rewire shows up as one entry, not as a
    # removal plus an addition.
    rewired: tuple[tuple[Subscription, Subscription], ...] = ()


class GraphDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: NodeChange = NodeChange()
    subscriptions: SubscriptionChange = SubscriptionChange()

    def __bool__(self) -> bool:
        return bool(
            self.nodes.added
            or self.nodes.removed
            or self.nodes.modified
            or self.nodes.replaced
            or self.subscriptions.added
            or self.subscriptions.removed
            or self.subscriptions.rewired
        )


def diff_graphs(old: Graph, new: Graph) -> GraphDiff:
    """Nodes and subscriptions that differ between two versions, each sorted for stable output.

    A node is matched by id. If its instance changed it is `replaced`, with the history record when
    there is one; otherwise any other changed field makes it `modified`, listing those fields.
    """
    before = {node.id: node for node in old.nodes}
    after = {node.id: node for node in new.nodes}
    kept = sorted(before.keys() & after.keys())
    records = {(record.old_instance_id, record.new_instance_id): record for record in new.replacements}
    replaced = tuple(
        records.get((before[node_id].instance_id, after[node_id].instance_id))
        or _replacement(before[node_id], after[node_id], new.version)
        for node_id in kept
        if before[node_id].instance_id != after[node_id].instance_id
    )
    modified = tuple(
        NodeModification(node=node_id, fields=_changed_fields(before[node_id], after[node_id]))
        for node_id in kept
        if before[node_id].instance_id == after[node_id].instance_id and before[node_id] != after[node_id]
    )
    old_inputs = {(sub.target_node, sub.target_port): sub for sub in old.subscriptions}
    new_inputs = {(sub.target_node, sub.target_port): sub for sub in new.subscriptions}
    rewired = tuple(
        sorted(
            ((old_inputs[key], new_inputs[key]) for key in old_inputs.keys() & new_inputs.keys() if old_inputs[key] != new_inputs[key]),
            key=lambda pair: (pair[0].target_node, pair[0].target_port),
        )
    )
    rewired_old = {pair[0] for pair in rewired}
    rewired_new = {pair[1] for pair in rewired}
    return GraphDiff(
        nodes=NodeChange(
            added=tuple(sorted(after.keys() - before.keys())),
            removed=tuple(sorted(before.keys() - after.keys())),
            modified=modified,
            replaced=replaced,
        ),
        subscriptions=SubscriptionChange(
            added=tuple(sub for sub in new.subscriptions if sub not in old.subscriptions and sub not in rewired_new),
            removed=tuple(sub for sub in old.subscriptions if sub not in new.subscriptions and sub not in rewired_old),
            rewired=rewired,
        ),
    )


def _changed_fields(old: NodeSpec, new: NodeSpec) -> tuple[str, ...]:
    return tuple(name for name in NodeSpec.model_fields if getattr(old, name) != getattr(new, name))


def _replacement(old: NodeSpec, new: NodeSpec, version: int) -> Replacement:
    # Versions compared across several patches with more than one replacement in between, or a node
    # removed and added again under its id later: no single record spans them, so describe the ends.
    return Replacement(
        node=new.id,
        version=max(version, 1),
        old_instance_id=old.instance_id,
        new_instance_id=new.instance_id,
        old_type=old.type,
        new_type=new.type,
        old_code=old.code,
        new_code=new.code,
    )
