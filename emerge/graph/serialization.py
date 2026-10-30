"""Graph and patch to dict/JSON and back.

Output is deterministic: keys are sorted, list order follows the graph's own lists, and equal
graphs serialize to identical bytes, so stored versions can be compared directly.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from emerge.exceptions import GraphValidationError
from emerge.graph.edge import StartEdge, start_edges_from_after
from emerge.graph.graph import Graph
from emerge.graph.patch import GraphPatch
from emerge.graph.ports import thaw

# 2 added node instances, pinned code versions and the replacement history; 3 replaced `after` with
# start edges and added the instance description. Older data still loads: `after` becomes the start
# edge it always meant (each listed node done), and a version 1 node becomes the first instance of
# its id, with no pinned code and no history.
SCHEMA_VERSION = 3
_READABLE = frozenset({1, 2, 3})


def graph_to_dict(graph: Graph) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "version": graph.version,
        "nodes": [
            {
                "id": node.id,
                "type": node.type,
                "params": thaw(node.params),
                "start_edges": [_edge_to_dict(edge) for edge in node.start_edges],
                "description": node.description,
                "instance_id": node.instance_id,
                "code": node.code,
            }
            for node in graph.nodes
        ],
        "subscriptions": [
            {
                "source_node": sub.source_node,
                "source_port": sub.source_port,
                "target_node": sub.target_node,
                "target_port": sub.target_port,
            }
            for sub in graph.subscriptions
        ],
        "replacements": [record.model_dump() for record in graph.replacements],
    }


def graph_from_dict(data: dict[str, Any]) -> Graph:
    schema = _check_schema(data)
    try:
        return Graph.model_validate(
            {
                "version": data.get("version", 0),
                "nodes": tuple(_upgrade(node, schema) for node in data.get("nodes", ())),
                "subscriptions": tuple(data.get("subscriptions", ())),
                "replacements": tuple(data.get("replacements", ())),
            }
        )
    except ValidationError as e:
        raise GraphValidationError(f"invalid graph data: {e}") from None


def dumps_graph(graph: Graph) -> str:
    return json.dumps(graph_to_dict(graph), ensure_ascii=False, sort_keys=True)


def loads_graph(text: str) -> Graph:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise GraphValidationError(f"invalid graph JSON: {e}") from None
    return graph_from_dict(data)


def patch_to_dict(patch: GraphPatch) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "edits": [
            {
                "op": edit.op,
                "node": edit.node,
                "type": edit.type,
                "params": thaw(edit.params),
                "start_edges": [_edge_to_dict(edge) for edge in edit.start_edges],
                "description": edit.description,
                "code": edit.code,
                "source": edit.source,
                "target": edit.target,
            }
            for edit in patch.edits
        ],
    }


def patch_from_dict(data: dict[str, Any]) -> GraphPatch:
    schema = _check_schema(data)
    try:
        return GraphPatch.model_validate({"edits": tuple(_upgrade_edit(edit, schema) for edit in data.get("edits", ()))})
    except ValidationError as e:
        raise GraphValidationError(f"invalid patch data: {e}") from None


def dumps_patch(patch: GraphPatch) -> str:
    return json.dumps(patch_to_dict(patch), ensure_ascii=False, sort_keys=True)


def loads_patch(text: str) -> GraphPatch:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise GraphValidationError(f"invalid patch JSON: {e}") from None
    return patch_from_dict(data)


def _check_schema(data: dict[str, Any]) -> int:
    version = data.get("schema_version")
    if version not in _READABLE:
        raise GraphValidationError(f"unsupported schema_version {version!r}; expected one of {sorted(_READABLE)}")
    return version


def _edge_to_dict(edge: StartEdge) -> dict[str, Any]:
    return {"sources": [{"node": source.node, "outcome": source.outcome} for source in edge.sources]}


def _upgrade(item: Any, schema: int) -> Any:
    """A node or edit as schema 3 has it: `after` becomes the one start edge it stood for."""
    if schema >= 3 or not isinstance(item, dict) or "after" not in item:
        return item
    after = item["after"]
    # A string would iterate as one-letter node ids.
    if not isinstance(after, (list, tuple)):
        raise GraphValidationError(f"invalid 'after' in schema {schema} data: {after!r}")
    upgraded = {key: value for key, value in item.items() if key != "after"}
    upgraded["start_edges"] = [_edge_to_dict(edge) for edge in start_edges_from_after(after)]
    return upgraded


def _upgrade_edit(edit: Any, schema: int) -> Any:
    if schema >= 3 or not isinstance(edit, dict):
        return edit
    upgraded = _upgrade(edit, schema)
    # set_after was the only way to change start conditions, and set_start_edges replaces it whole.
    if upgraded.get("op") == "set_after":
        upgraded = {**upgraded, "op": "set_start_edges", "start_edges": upgraded.get("start_edges", [])}
    return upgraded
