"""Structural checks over a graph, returning issues instead of raising.

Errors block a graph; warnings do not. Every issue names a code and where it was found, so callers
can report it, and `builder` / `patch` can turn errors into a GraphValidationError.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from emerge.exceptions import GraphValidationError
from emerge.graph.edge import Subscription
from emerge.graph.graph import Graph

if TYPE_CHECKING:
    from emerge.graph.ports import Port
    from emerge.graph.registry import NodeEntry


class IssueCode(StrEnum):
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    UNKNOWN_CODE_VERSION = "UNKNOWN_CODE_VERSION"
    PARAM_INVALID = "PARAM_INVALID"
    ENDPOINT_MISSING = "ENDPOINT_MISSING"
    INPUT_MULTIPLE_SOURCES = "INPUT_MULTIPLE_SOURCES"
    START_UNKNOWN_NODE = "START_UNKNOWN_NODE"
    START_UNKNOWN_OUTCOME = "START_UNKNOWN_OUTCOME"
    START_EDGE_EMPTY = "START_EDGE_EMPTY"
    # One edge naming a source twice; a graph file maps each source to one outcome.
    START_DUPLICATE_SOURCE = "START_DUPLICATE_SOURCE"
    START_CYCLE = "START_CYCLE"
    DUPLICATE_ID = "DUPLICATE_ID"
    DUPLICATE_INSTANCE = "DUPLICATE_INSTANCE"
    # A replacement left a kept subscription with mismatched type tags; raised by patch.apply.
    REPLACEMENT_PORT_MISMATCH = "REPLACEMENT_PORT_MISMATCH"
    TYPE_TAG_MISMATCH = "TYPE_TAG_MISMATCH"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


_WARNINGS = frozenset({IssueCode.TYPE_TAG_MISMATCH})


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: IssueCode
    message: str
    node: str | None = None
    port: str | None = None
    subscription: Subscription | None = None
    # Closed path for START_CYCLE, e.g. ("a", "b", "a").
    path: tuple[str, ...] = ()

    @property
    def severity(self) -> Severity:
        return Severity.WARNING if self.code in _WARNINGS else Severity.ERROR


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[Issue, ...] = ()

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is Severity.WARNING)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        errors = self.errors
        if errors:
            detail = "\n".join(f"  {issue.code}: {issue.message}" for issue in errors)
            raise GraphValidationError(f"graph has {len(errors)} error(s):\n{detail}")


class Catalog(Protocol):
    """The part of the registry validation needs."""

    def find_entry(self, name: str, code: str | None = None) -> "NodeEntry | None": ...


def validate(graph: Graph, catalog: Catalog) -> ValidationResult:
    issues: list[Issue] = []
    # Each instance is checked against the implementation it is pinned to, not the newest one.
    types = {node.id: _lookup(catalog, node.type, node.code) for node in graph.nodes}

    issues += _duplicate_ids(graph)
    issues += _duplicate_instances(graph)
    for node in graph.nodes:
        entry = types[node.id]
        if entry is None:
            if node.code is not None and catalog.find_entry(node.type) is not None:
                issues.append(
                    Issue(
                        code=IssueCode.UNKNOWN_CODE_VERSION,
                        message=f"node type {node.type!r} has no registered version {node.code!r}",
                        node=node.id,
                    )
                )
            else:
                issues.append(Issue(code=IssueCode.UNKNOWN_TYPE, message=f"unknown node type {node.type!r}", node=node.id))
            continue
        issues += _params(node.id, node.params, entry)

    issues += _subscriptions(graph, types)
    issues += _start_edges(graph, types)
    return ValidationResult(issues=tuple(issues))


def _lookup(catalog: Catalog, type_name: str, code: str | None) -> "NodeEntry | None":
    return catalog.find_entry(type_name, code)


def _duplicate_ids(graph: Graph) -> list[Issue]:
    seen: set[str] = set()
    issues: list[Issue] = []
    for node in graph.nodes:
        if node.id in seen:
            issues.append(Issue(code=IssueCode.DUPLICATE_ID, message=f"node id {node.id!r} is used more than once", node=node.id))
        seen.add(node.id)
    return issues


def _duplicate_instances(graph: Graph) -> list[Issue]:
    # An instance that has already existed cannot come back: its old messages would count again.
    past = {record.old_instance_id for record in graph.replacements}
    seen: set[str] = set()
    issues: list[Issue] = []
    for node in graph.nodes:
        if node.instance_id in seen or node.instance_id in past:
            issues.append(
                Issue(
                    code=IssueCode.DUPLICATE_INSTANCE,
                    message=f"instance {node.instance_id!r} of node {node.id!r} is already used",
                    node=node.id,
                )
            )
        seen.add(node.instance_id)
    return issues


def _params(node_id: str, params: Mapping[str, Any], entry: "NodeEntry") -> list[Issue]:
    model = entry.params_model
    if model is None:
        if params:
            return [Issue(code=IssueCode.PARAM_INVALID, message="node type takes no parameters", node=node_id)]
        return []
    try:
        model.model_validate(params)
    except ValidationError as e:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}" for item in e.errors()
        )
        return [Issue(code=IssueCode.PARAM_INVALID, message=detail, node=node_id)]
    return []


def _subscriptions(graph: Graph, types: dict[str, "NodeEntry | None"]) -> list[Issue]:
    issues: list[Issue] = []
    used_inputs: set[tuple[str, str]] = set()
    for sub in graph.subscriptions:
        source = _port(types.get(sub.source_node), sub.source_port, output=True)
        target = _port(types.get(sub.target_node), sub.target_port, output=False)
        # An unknown type already failed UNKNOWN_TYPE; its ports are unknowable, not missing.
        if sub.source_node not in types:
            issues.append(_missing_endpoint(sub, f"source node {sub.source_node!r} is not in the graph"))
        elif source is None and types[sub.source_node] is not None:
            issues.append(_missing_endpoint(sub, f"node {sub.source_node!r} has no output port {sub.source_port!r}"))
        if sub.target_node not in types:
            issues.append(_missing_endpoint(sub, f"target node {sub.target_node!r} is not in the graph"))
        elif target is None and types[sub.target_node] is not None:
            issues.append(_missing_endpoint(sub, f"node {sub.target_node!r} has no input port {sub.target_port!r}"))
        key = (sub.target_node, sub.target_port)
        if key in used_inputs:
            issues.append(
                Issue(
                    code=IssueCode.INPUT_MULTIPLE_SOURCES,
                    message=f"input {sub.target_node}.{sub.target_port} already has a source",
                    node=sub.target_node,
                    port=sub.target_port,
                    subscription=sub,
                )
            )
        used_inputs.add(key)
        if source is not None and target is not None and source.type_tag != target.type_tag:
            issues.append(
                Issue(
                    code=IssueCode.TYPE_TAG_MISMATCH,
                    message=(
                        f"{sub.source_node}.{sub.source_port} produces {source.type_tag!r} "
                        f"but {sub.target_node}.{sub.target_port} expects {target.type_tag!r}"
                    ),
                    subscription=sub,
                )
            )
    return issues


def _missing_endpoint(sub: Subscription, message: str) -> Issue:
    return Issue(code=IssueCode.ENDPOINT_MISSING, message=message, subscription=sub)


def _port(entry: "NodeEntry | None", name: str, *, output: bool) -> "Port | None":
    if entry is None:
        return None
    ports = entry.outputs if output else entry.inputs
    return next((port for port in ports if port.name == name), None)


def _start_edges(graph: Graph, types: dict[str, "NodeEntry | None"]) -> list[Issue]:
    issues: list[Issue] = []
    for node in graph.nodes:
        for index, edge in enumerate(node.start_edges):
            if not edge.sources:
                issues.append(
                    Issue(
                        code=IssueCode.START_EDGE_EMPTY,
                        message=f"start edge {index} of node {node.id!r} has no sources and can never fire",
                        node=node.id,
                    )
                )
            named = [source.node for source in edge.sources]
            for repeated in sorted({node_id for node_id in named if named.count(node_id) > 1}):
                issues.append(
                    Issue(
                        code=IssueCode.START_DUPLICATE_SOURCE,
                        message=f"start edge {index} of node {node.id!r} names {repeated!r} more than once",
                        node=node.id,
                    )
                )
            for source in edge.sources:
                if source.node not in types:
                    issues.append(
                        Issue(
                            code=IssueCode.START_UNKNOWN_NODE,
                            message=f"node {node.id!r} starts after unknown node {source.node!r}",
                            node=node.id,
                        )
                    )
                    continue
                entry = types[source.node]
                # Outcomes belong to the source's type; an unknown type already failed UNKNOWN_TYPE.
                if source.outcome is not None and entry is not None and source.outcome not in entry.outcomes:
                    issues.append(
                        Issue(
                            code=IssueCode.START_UNKNOWN_OUTCOME,
                            message=(
                                f"node {node.id!r} waits for {source.node!r} to finish {source.outcome!r}, "
                                f"but type {entry.name!r} declares only {', '.join(entry.outcomes)}"
                            ),
                            node=node.id,
                        )
                    )
    for cycle in graph.start_edge_cycles():
        issues.append(
            Issue(
                code=IssueCode.START_CYCLE,
                message="start edges form a cycle: " + " -> ".join(cycle),
                node=cycle[0],
                path=cycle,
            )
        )
    return issues
