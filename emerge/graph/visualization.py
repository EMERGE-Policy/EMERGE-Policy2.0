"""Text renderings of a graph: Mermaid for documents, plain text for the terminal.

Both take optional overlays for display: each node's lifecycle state, and the outcome each finished
node gave. Overlays are read, never written back, and the graph is not modified. Both show each
node's current instance, its pinned code version, the instances it replaced, and on every start
edge the outcome it asks for.
"""

from __future__ import annotations

from collections.abc import Mapping

from emerge.graph.edge import StartSource
from emerge.graph.graph import Graph, NodeSpec, Replacement


def to_mermaid(
    graph: Graph,
    states: Mapping[str, str] | None = None,
    outcomes: Mapping[str, str] | None = None,
) -> str:
    """Mermaid flowchart: subscriptions are arrows, start edges dotted arrows labelled with the
    outcome they ask for ("done" for any). An edge with several sources, all of which must be met,
    joins them at a small "and" circle; a node's separate edges each point at it on their own."""
    states = states or {}
    outcomes = outcomes or {}
    ids = {node.id: f"n{index}" for index, node in enumerate(graph.nodes)}
    # Mermaid reserves these words, and a node named after one would otherwise break the diagram.
    reserved = {"end", "graph", "subgraph", "class", "click"}

    def ref(node_id: str) -> str:
        if node_id not in ids:
            # A dangling reference in an unvalidated graph still has to render.
            ids[node_id] = f"x{len(ids)}"
        return f"{ids[node_id]}_" if node_id in reserved else ids[node_id]

    lines = ["flowchart LR"]
    for node in graph.nodes:
        status = _status(node.id, states, outcomes)
        label = _label(f"{node.id}<br/>{_type(node)}<br/>{node.instance_id}" + (f"<br/>{status}" if status else ""))
        style = ":::skipped" if states.get(node.id) == "skipped" else ""
        lines.append(f'    {ref(node.id)}["{label}"]{style}')
    for sub in graph.subscriptions:
        ports = f"{sub.source_port}->{sub.target_port}"
        lines.append(f'    {ref(sub.source_node)} -- "{_label(ports)}" --> {ref(sub.target_node)}')
    joins = 0
    for node in graph.nodes:
        for edge in node.start_edges:
            if len(edge.sources) == 1:
                source = edge.sources[0]
                lines.append(f'    {ref(source.node)} -.->|"{_label(_requirement(source))}"| {ref(node.id)}')
            elif edge.sources:
                join = f"j{joins}"
                joins += 1
                lines.append(f'    {join}(("and"))')
                for source in edge.sources:
                    lines.append(f'    {ref(source.node)} -.->|"{_label(_requirement(source))}"| {join}')
                lines.append(f"    {join} -.-> {ref(node.id)}")
    # Replaced instances are drawn as their own shapes, chained oldest to newest into the current
    # node, so the history is visible without being mistaken for a live node.
    for index, record in enumerate(graph.replacements):
        old = f"r{index}"
        label = _label(f"{record.old_instance_id}<br/>{_versioned(record.old_type, record.old_code)}<br/>replaced v{record.version}")
        lines.append(f'    {old}(["{label}"]):::replaced')
        later = _next_replacement(graph, index)
        target = f"r{later}" if later is not None else ref(record.node)
        lines.append(f"    {old} == replaced by ==> {target}")
    if graph.replacements:
        lines.append("    classDef replaced stroke-dasharray: 4 3,opacity:0.7")
    if any(states.get(node.id) == "skipped" for node in graph.nodes):
        lines.append("    classDef skipped stroke-dasharray: 2 2,opacity:0.5")
    return "\n".join(lines)


def to_text(
    graph: Graph,
    states: Mapping[str, str] | None = None,
    outcomes: Mapping[str, str] | None = None,
) -> str:
    """Plain-text listing with a per-node block; also usable without a terminal."""
    states = states or {}
    outcomes = outcomes or {}
    lines = [f"graph v{graph.version}  nodes={len(graph.nodes)}  subscriptions={len(graph.subscriptions)}"]
    for node in graph.nodes:
        status = _status(node.id, states, outcomes)
        suffix = f"  [{status}]" if status else ""
        lines.append(f"  {node.id} ({_type(node)}) instance {node.instance_id}{suffix}")
        if node.description:
            lines.append(f"      about: {node.description}")
        for record in graph.replacements_of(node.id):
            lines.append(
                f"      replaced v{record.version}: {record.old_instance_id} "
                f"({_versioned(record.old_type, record.old_code)}) -> {record.new_instance_id} "
                f"({_versioned(record.new_type, record.new_code)})"
            )
        for sub in graph.subscriptions:
            if sub.target_node == node.id:
                lines.append(f"      in  {sub.target_port} <- {sub.source_node}.{sub.source_port}")
            if sub.source_node == node.id:
                lines.append(f"      out {sub.source_port} -> {sub.target_node}.{sub.target_port}")
        # Each line is one way to start: all sources on it, as required; any one line suffices.
        for index, edge in enumerate(node.start_edges):
            lines.append(f"      {'starts after' if index == 0 else 'or after'} {edge}")
    roots = graph.node_ids_without_start_dependencies()
    if roots:
        lines.append(f"  roots: {', '.join(roots)}")
    return "\n".join(lines)


def _status(node_id: str, states: Mapping[str, str], outcomes: Mapping[str, str]) -> str:
    state, outcome = states.get(node_id), outcomes.get(node_id)
    if outcome is None:
        return state or ""
    return f"{state or 'finished'}: {outcome}"


def _requirement(source: StartSource) -> str:
    return "done" if source.outcome is None else source.outcome


def _type(node: NodeSpec) -> str:
    return _versioned(node.type, node.code)


def _versioned(node_type: str, code: str | None) -> str:
    return node_type if code is None else f"{node_type}@{code}"


def _next_replacement(graph: Graph, index: int) -> int | None:
    """The record that replaced the instance record `index` brought in, if any."""
    current: Replacement = graph.replacements[index]
    for later in range(index + 1, len(graph.replacements)):
        if graph.replacements[later].old_instance_id == current.new_instance_id:
            return later
    return None


def _label(text: str) -> str:
    # Mermaid ends a quoted label at the first quote it sees.
    return text.replace('"', "#quot;")
