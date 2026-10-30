"""Fluent graph assembly for scripts and agent tools.

The builder is mutable and short-lived: it accumulates node instances and connections, then
`build()` freezes them into a validated `Graph`. Port type tags come from the registry, so a
connection only names its two endpoints.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from emerge.graph.edge import StartEdge, Subscription, parse_endpoint, start_edges_from_after
from emerge.graph.graph import Graph, NodeSpec
from emerge.graph.registry import NodeRegistry, get_active_registry
from emerge.graph.validation import ValidationResult, validate

logger = logging.getLogger(__name__)


class GraphBuilder:
    def __init__(self, registry: NodeRegistry | None = None) -> None:
        self.registry = registry if registry is not None else get_active_registry()
        # A list, not a dict: a repeated id must reach validation and be reported as DUPLICATE_ID
        # instead of the later definition silently replacing the earlier one.
        self._nodes: list[NodeSpec] = []
        self._subscriptions: list[Subscription] = []
        # Instance-level live sources. They are not part of the graph, so they are only kept here
        # for the impact query while the builder exists; the durable declaration is on Node.reads.
        self._reads: dict[str, tuple[str, ...]] = {}

    def add_node(
        self,
        node_id: str,
        node_type: str,
        *,
        params: dict[str, Any] | None = None,
        start_edges: Iterable[StartEdge] = (),
        after: Iterable[str] = (),
        description: str = "",
        code: str | None = None,
    ) -> GraphBuilder:
        """Place a node. A generated type is pinned to `code`, or to its current version if omitted.

        `after=[...]` is shorthand for one more start edge on which each listed node only has to be
        done; `start_edges` says which outcome each source must end with.
        """
        self._nodes.append(
            NodeSpec(
                id=node_id,
                type=node_type,
                params=params or {},
                start_edges=(*start_edges, *start_edges_from_after(after)),
                description=description,
                code=self.registry.pin(node_type, code),
            )
        )
        return self

    def declare_external_read(self, node_id: str, source: str) -> GraphBuilder:
        """Declare a live source this instance reads directly; not a graph edge and not schedulable."""
        self._reads[node_id] = (*self._reads.get(node_id, ()), source)
        return self

    def connect_ports(self, source: str, target: str) -> GraphBuilder:
        """Wire "node.port" to "node.port". The target input may be connected only once."""
        subscription = _subscription(source, target)
        if subscription not in self._subscriptions:
            self._subscriptions.append(subscription)
        return self

    def connect_port_pairs(self, pairs: Iterable[tuple[str, str]]) -> GraphBuilder:
        for source, target in pairs:
            self.connect_ports(source, target)
        return self

    def validate_graph(self) -> ValidationResult:
        return validate(self.build_unvalidated_graph(), self.registry)

    def build(self) -> Graph:
        """Freeze and validate. Errors raise GraphValidationError; warnings are logged."""
        result = self.validate_graph()
        result.raise_if_invalid()
        for warning in result.warnings:
            logger.warning("graph warning %s: %s", warning.code, warning.message)
        return self.build_unvalidated_graph()

    def build_unvalidated_graph(self) -> Graph:
        """The graph as assembled, without validating it."""
        return Graph(version=0, nodes=tuple(self._nodes), subscriptions=tuple(self._subscriptions))

    def external_reads_by_node_type(self) -> dict[str, tuple[str, ...]]:
        """Live sources per node type, from the registry plus instance declarations.

        Keyed by type, so an instance declaration is counted for every instance of that type: an
        over-approximation, which is the safe direction for "what would this change disturb".
        """
        types = {node.id: node.type for node in self._nodes}
        merged = {name: names for name, names in self.registry.external_reads_by_node_type().items()}
        for node_id, sources in self._reads.items():
            node_type = types.get(node_id)
            if node_type is not None:
                merged[node_type] = tuple(dict.fromkeys((*merged.get(node_type, ()), *sources)))
        return merged


def _subscription(source: str, target: str) -> Subscription:
    source_node, source_port = parse_endpoint(source)
    target_node, target_port = parse_endpoint(target)
    return Subscription(
        source_node=source_node,
        source_port=source_port,
        target_node=target_node,
        target_port=target_port,
    )


def build_graph(
    nodes: Iterable[tuple[str, str]],
    connections: Iterable[tuple[str, str]] = (),
    *,
    registry: NodeRegistry | None = None,
) -> Graph:
    """Shorthand for the common case: (id, type) pairs and endpoint pairs."""
    builder = GraphBuilder(registry)
    for node_id, node_type in nodes:
        builder.add_node(node_id, node_type)
    builder.connect_port_pairs(connections)
    return builder.build()
