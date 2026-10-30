"""The graph file: the form of a graph the main agent reads and edits.

A graph file (`"format": "emerge.graph-spec/v1"`) states the graph as it should be. For each node
id: `type`, `code` (generated types only), `description`, `params`, `inputs` (input port -> the
"node.port" output that feeds it) and `start_when` (`any_of` a list of `all_of` maps, source node ->
outcome, "*" for any outcome; left out, the node is a root). It leaves out what the framework owns:
instance ids, graph versions and the replacement history.

Handing an edited file back is a change request: `patch_from_graph_spec` compares it with the
current graph and returns the patch that gets there, and `apply_graph_patch` checks that patch
against the run's node states like any other. So every rule about what may change lives once, in
patch.py, and a file can do nothing a patch could not. Running a node again as a new instance with
nothing else changed is an action rather than a state, so it is the `replace` argument instead of
something written in the file.

serialization.py keeps the other form, lossless, for storage.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from emerge.exceptions import GraphValidationError
from emerge.graph.builder import GraphBuilder
from emerge.graph.edge import ANY_OUTCOME, StartEdge, StartSource, Subscription, parse_endpoint
from emerge.graph.graph import Graph, NodeSpec
from emerge.graph.patch import GraphPatch, apply_graph_patch
from emerge.graph.ports import NAME_PATTERN, FrozenDict, FrozenJson, freeze, thaw
from emerge.graph.registry import NodeRegistry

GRAPH_SPEC_FORMAT = "emerge.graph-spec/v1"
_NAME = re.compile(NAME_PATTERN)
# Down to each node's own fields every entry gets a line of its own; below that, a value that fits
# in this many columns stays on one line, the way graph files are written by hand.
_LINE_WIDTH = 100
_EXPANDED_LEVELS = 3


def _require_name(value: str, what: str) -> None:
    if not _NAME.match(value):
        raise ValueError(f"{what} {value!r} must be non-empty and contain no dots or whitespace")


def _outcomes_by_source(value: dict[str, str]) -> Mapping[str, str]:
    for node_id, outcome in value.items():
        _require_name(node_id, "start source")
        _require_name(outcome, f"outcome asked of {node_id!r}")
    return freeze(value)


def _sources_by_input(value: dict[str, str]) -> Mapping[str, str]:
    for port, endpoint in value.items():
        _require_name(port, "input port")
        node_id, dot, source_port = endpoint.rpartition(".")
        if not (dot and _NAME.match(node_id) and _NAME.match(source_port)):
            raise ValueError(f"input {port!r} must name the output feeding it as 'node.port', not {endpoint!r}")
    return freeze(value)


def _nodes_by_id(value: dict[str, GraphSpecNode]) -> Mapping[str, GraphSpecNode]:
    for node_id in value:
        _require_name(node_id, "node id")
    return freeze(value)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StartCondition(_Frozen):
    """One way to start: every source here has finished with the outcome it maps to, "*" for any."""

    all_of: Annotated[dict[str, str], Field(min_length=1), AfterValidator(_outcomes_by_source)]


class StartWhen(_Frozen):
    """The node starts once any one of these conditions holds, and only once."""

    # At least one: an empty list reads like "never", yet a node without conditions is a root, and a
    # root is written by leaving `start_when` out.
    any_of: tuple[StartCondition, ...] = Field(min_length=1)


class GraphSpecNode(_Frozen):
    type: str = Field(min_length=1)
    # Implementation version of a generated type. Left out, a new node is pinned to the current
    # version and an existing one keeps its own; another version runs the node as a new instance.
    code: str | None = Field(default=None, min_length=1)
    description: str = ""
    params: FrozenJson = Field(default_factory=FrozenDict)
    inputs: Annotated[dict[str, str], AfterValidator(_sources_by_input)] = Field(default_factory=FrozenDict)
    start_when: StartWhen | None = None

    def start_edges(self) -> tuple[StartEdge, ...]:
        if self.start_when is None:
            return ()
        return tuple(
            StartEdge(
                sources=tuple(
                    StartSource(node=node_id, outcome=None if outcome == ANY_OUTCOME else outcome)
                    for node_id, outcome in condition.all_of.items()
                )
            )
            for condition in self.start_when.any_of
        )


class GraphSpec(_Frozen):
    format: Literal["emerge.graph-spec/v1"]
    # The graph version the file was written from. Given, the file applies to that version only, so
    # an edit made against a graph that has changed since is refused instead of merged.
    graph_version: int | None = Field(default=None, ge=0)
    nodes: Annotated[dict[str, GraphSpecNode], AfterValidator(_nodes_by_id)] = Field(default_factory=FrozenDict)

    @model_validator(mode="after")
    def _references_resolve(self) -> GraphSpec:
        # The file is the whole graph, so every input and start condition must name a node in it.
        # Checked here rather than left to graph validation: removing a node also drops the
        # subscriptions from it, so an input still naming that node would vanish without a word.
        missing = []
        for node_id, node in self.nodes.items():
            for port, endpoint in node.inputs.items():
                if endpoint.rpartition(".")[0] not in self.nodes:
                    missing.append(f"nodes.{node_id}.inputs.{port} reads {endpoint!r}")
            for index, condition in enumerate(node.start_when.any_of if node.start_when else ()):
                for source in condition.all_of:
                    if source not in self.nodes:
                        missing.append(f"nodes.{node_id}.start_when.any_of.{index} waits for {source!r}")
        if missing:
            raise ValueError("these name a node that is not in the file: " + "; ".join(missing))
        return self

    def subscriptions(self) -> tuple[Subscription, ...]:
        subscriptions = []
        for node_id, node in self.nodes.items():
            for port, endpoint in node.inputs.items():
                source_node, source_port = parse_endpoint(endpoint)
                subscriptions.append(
                    Subscription(source_node=source_node, source_port=source_port, target_node=node_id, target_port=port)
                )
        return tuple(subscriptions)


# -- graph -> file --------------------------------------------------------------------------


def graph_spec_from_graph(graph: Graph) -> GraphSpec:
    """The file for `graph`, at its version; handed back unedited, it changes nothing.

    Every valid graph has a file. A graph that validation would reject may not: two sources on one
    input, or one node named twice on a start edge, have no way to be written as JSON keys.
    """
    nodes: dict[str, GraphSpecNode] = {}
    try:
        for node in graph.nodes:
            if node.id in nodes:
                raise GraphValidationError(f"node id {node.id!r} is used more than once; a graph file cannot hold both")
            nodes[node.id] = _spec_node(graph, node)
        return GraphSpec(format=GRAPH_SPEC_FORMAT, graph_version=graph.version, nodes=nodes)
    except ValidationError as e:
        raise GraphValidationError(f"the graph cannot be written as a graph file: {e}") from None


def _spec_node(graph: Graph, node: NodeSpec) -> GraphSpecNode:
    inputs: dict[str, str] = {}
    for sub in graph.subscriptions:
        if sub.target_node == node.id:
            if sub.target_port in inputs:
                raise GraphValidationError(f"input {node.id}.{sub.target_port} has two sources; a graph file holds one")
            inputs[sub.target_port] = f"{sub.source_node}.{sub.source_port}"
    conditions = []
    for edge in node.start_edges:
        repeated = [node_id for node_id, count in Counter(source.node for source in edge.sources).items() if count > 1]
        if repeated or not edge.sources:
            raise GraphValidationError(f"a start edge of {node.id!r} cannot be written as a graph file condition: {edge}")
        conditions.append(
            StartCondition(
                all_of={source.node: ANY_OUTCOME if source.outcome is None else source.outcome for source in edge.sources}
            )
        )
    return GraphSpecNode(
        type=node.type,
        code=node.code,
        description=node.description,
        params=node.params,
        inputs=inputs,
        start_when=StartWhen(any_of=tuple(conditions)) if conditions else None,
    )


def dumps_graph_spec(spec: GraphSpec) -> str:
    """The file as text: two-space indents, keys in a fixed order, optional ones only when set.

    Writing the same spec twice gives the same bytes, and reading the text back gives the same spec.
    """
    nodes: dict[str, Any] = {}
    for node_id, node in spec.nodes.items():
        entry: dict[str, Any] = {"type": node.type}
        if node.code is not None:
            entry["code"] = node.code
        entry["description"] = node.description
        entry["params"] = thaw(node.params)
        if node.inputs:
            entry["inputs"] = dict(node.inputs)
        if node.start_when is not None:
            entry["start_when"] = {"any_of": [{"all_of": dict(c.all_of)} for c in node.start_when.any_of]}
        nodes[node_id] = entry
    data: dict[str, Any] = {"format": spec.format}
    if spec.graph_version is not None:
        data["graph_version"] = spec.graph_version
    data["nodes"] = nodes
    return _render(data, depth=0, column=0) + "\n"


def _render(value: Any, *, depth: int, column: int) -> str:
    """JSON for `value`, which starts at `column` on a line nested `depth` levels deep."""
    flat = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    # Strictly less, so the comma that may follow still fits.
    if not isinstance(value, (dict, list)) or not value or (depth >= _EXPANDED_LEVELS and column + len(flat) < _LINE_WIDTH):
        return flat
    pad, inner = "  " * depth, "  " * (depth + 1)
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            head = f"{inner}{json.dumps(key, ensure_ascii=False)}: "
            lines.append(head + _render(item, depth=depth + 1, column=len(head)))
        return "{\n" + ",\n".join(lines) + f"\n{pad}}}"
    for item in value:
        lines.append(inner + _render(item, depth=depth + 1, column=len(inner)))
    return "[\n" + ",\n".join(lines) + f"\n{pad}]"


# -- file -> graph ----------------------------------------------------------------------------


def loads_graph_spec(text: str) -> GraphSpec:
    """Parse and check a graph file.

    Three passes, each reporting everything it finds: JSON syntax and repeated keys, then the
    fields, then references to nodes missing from the file. A pass runs only if the earlier ones
    found nothing.
    """
    try:
        data = json.loads(text, object_pairs_hook=_refuse_duplicate_keys)
    except json.JSONDecodeError as e:
        raise GraphValidationError(f"graph file is not valid JSON: line {e.lineno} column {e.colno}: {e.msg}") from None
    except _DuplicateKey as e:
        raise GraphValidationError(f"graph file: {e}") from None
    try:
        return GraphSpec.model_validate(data)
    except ValidationError as e:
        lines = [f"  {'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}" for item in e.errors()]
        raise GraphValidationError("graph file has errors:\n" + "\n".join(lines)) from None


class _DuplicateKey(ValueError):
    pass


def _refuse_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # json.loads keeps the last of two equal keys. In a graph file that would drop a node, an input
    # or a start source without a word, so it is an error instead.
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"key {key!r} appears twice in one object")
        result[key] = value
    return result


def graph_from_graph_spec(spec: GraphSpec, registry: NodeRegistry | None = None) -> Graph:
    """A new graph at version 0, validated; the file's `graph_version` is ignored."""
    builder = GraphBuilder(registry)
    for node_id, node in spec.nodes.items():
        builder.add_node(
            node_id,
            node.type,
            params=node.params,
            start_edges=node.start_edges(),
            description=node.description,
            code=node.code,
        )
    for sub in spec.subscriptions():
        builder.connect_ports(*_endpoints(sub))
    return builder.build()


# -- edited file -> patch -------------------------------------------------------------------


def patch_from_graph_spec(graph: Graph, spec: GraphSpec, *, replace: Iterable[str] = ()) -> GraphPatch:
    """The patch that turns `graph` into what `spec` describes; empty when the two already agree.

    A node missing from the file is removed and a new one is added. A node whose `type` changed, or
    whose `code` names another version, runs as a new instance (`replace_node`), and so does each
    node listed in `replace`; other changed fields are set on the same instance. Inputs are compared
    one by one, so editing one line rewires one input. Whether each change is allowed in the run's
    current state is decided when the patch is applied, not here.
    """
    if spec.graph_version is not None and spec.graph_version != graph.version:
        raise GraphValidationError(
            f"the graph file was written from graph version {spec.graph_version}, but the graph is at version "
            f"{graph.version} now; take a fresh file of the current graph and make the edit again"
        )
    current = {node.id: node for node in graph.nodes}
    rerun = set(replace)
    unknown = sorted(rerun - (spec.nodes.keys() & current.keys()))
    if unknown:
        raise GraphValidationError(
            f"cannot replace {', '.join(map(repr, unknown))}: only a node in both the graph and the file has an "
            "instance to replace"
        )
    removed = [node_id for node_id in current if node_id not in spec.nodes]
    parts = [GraphPatch.remove_node(node_id) for node_id in removed]
    for node_id, wanted in spec.nodes.items():
        node = current.get(node_id)
        if node is None:
            parts.append(_added(node_id, wanted))
        elif node_id in rerun or wanted.type != node.type or (wanted.code is not None and wanted.code != node.code):
            parts += _replaced(node, wanted)
        else:
            parts += _updated(node, wanted)
    parts += _rewired(graph, spec, set(removed))
    return GraphPatch().combine_patches(*parts)


def apply_graph_spec(
    graph: Graph,
    spec: GraphSpec | str,
    registry: NodeRegistry | None = None,
    *,
    states: Mapping[str, str] | None = None,
    replace: Iterable[str] = (),
) -> Graph:
    """Commit an edited graph file, checked against the run's node states like any patch.

    Raises GraphValidationError and leaves `graph` as it was when the file is malformed, was taken
    from another graph version, asks for a change the node states rule out, or describes an
    invalid graph. A file that changes nothing gives back `graph` itself.
    """
    if isinstance(spec, str):
        spec = loads_graph_spec(spec)
    return apply_graph_patch(graph, patch_from_graph_spec(graph, spec, replace=replace), registry, states=states)


def _added(node_id: str, wanted: GraphSpecNode) -> GraphPatch:
    return GraphPatch.add_node(
        node_id,
        wanted.type,
        params=wanted.params,
        start_edges=wanted.start_edges(),
        description=wanted.description,
        code=wanted.code,
    )


def _replaced(node: NodeSpec, wanted: GraphSpecNode) -> list[GraphPatch]:
    # Omitted, `code` keeps the node's own version while the type stays; a new type starts on its
    # current version, because the old pin belongs to the old type.
    code = wanted.code if wanted.code is not None or wanted.type != node.type else node.code
    parts = [
        GraphPatch.replace_node(node.id, wanted.type, params=wanted.params, description=wanted.description, code=code)
    ]
    # A replacement keeps the start edges, which are fixed once the node starts; other conditions in
    # the file are a separate edit, accepted only while the node is still pending.
    if not _same_start_edges(wanted.start_edges(), node.start_edges):
        parts.append(GraphPatch.replace_start_edges(node.id, wanted.start_edges()))
    return parts


def _updated(node: NodeSpec, wanted: GraphSpecNode) -> list[GraphPatch]:
    parts = []
    if wanted.params != node.params:
        parts.append(GraphPatch.replace_node_params(node.id, wanted.params))
    if wanted.description != node.description:
        parts.append(GraphPatch.set_node_description(node.id, wanted.description))
    if not _same_start_edges(wanted.start_edges(), node.start_edges):
        parts.append(GraphPatch.replace_start_edges(node.id, wanted.start_edges()))
    return parts


def _same_start_edges(a: tuple[StartEdge, ...], b: tuple[StartEdge, ...]) -> bool:
    # Every source on an edge must hold and any one edge will do, so order means nothing: the same
    # conditions written in another order are no change, and must not be refused as one for a node
    # that has already started.
    def conditions(edges: tuple[StartEdge, ...]) -> set[frozenset[tuple[str, str | None]]]:
        return {frozenset((source.node, source.outcome) for source in edge.sources) for edge in edges}

    return conditions(a) == conditions(b)


def _rewired(graph: Graph, spec: GraphSpec, removed: set[str]) -> list[GraphPatch]:
    wanted = spec.subscriptions()
    # A removed node takes its own subscriptions with it.
    dropped = [sub for sub in graph.subscriptions if sub not in wanted and not removed & {sub.source_node, sub.target_node}]
    added = [sub for sub in wanted if sub not in graph.subscriptions]
    return [GraphPatch.disconnect(*_endpoints(sub)) for sub in dropped] + [
        GraphPatch.connect(*_endpoints(sub)) for sub in added
    ]


def _endpoints(sub: Subscription) -> tuple[str, str]:
    return f"{sub.source_node}.{sub.source_port}", f"{sub.target_node}.{sub.target_port}"
