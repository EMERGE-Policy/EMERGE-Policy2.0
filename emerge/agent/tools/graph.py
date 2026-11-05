"""The graph of the current run: commit the edited graph file, drop the edits, look at the graph,
and register generated node code.

The graph file `runs/<run_id>/graph.json` is the main agent's draft, edited with the file tools.
`commit_graph` hands it to the runtime, which works out what changed and checks it against the
node states of that moment. Patches stay a program interface (the interface, tests, reopening) and
are not offered here: one way to change the graph is enough.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from emerge.agent.tool import Tool, tool
from emerge.agent.tools import ToolContext
from emerge.exceptions import GraphValidationError, RegistryError, ToolError
from emerge.graph.registry import NodeRegistry
from emerge.graph.spec import loads_graph_spec
from emerge.graph.visualization import to_text
from emerge.runtime.mutations import GraphCommitResult
from emerge.storage.artifacts import code_path, list_generated_versions, write_generated_code

# Tools here that change the graph, which the stale-call check covers.
GUARDED = frozenset({"commit_graph", "reset_graph"})

_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def make_tools(ctx: ToolContext) -> list[Tool]:
    runtime = ctx.runtime
    # The last refused commit, so the same file is not sent again unchanged: (run, version, file, replace).
    refused: dict[str, tuple[int, str, tuple[str, ...]]] = {}

    @tool
    async def commit_graph(dry_run: bool = False, replace: list[str] | None = None) -> str:
        """Commit the current run's graph file (runs/<run_id>/graph.json) so the run follows it.

        Reports whether it was committed, the new graph version, what changed and where every node
        that has not started stands: starts now, keeps waiting, can never start, or starts once its
        old instance has stopped. A refused commit leaves the file exactly as you wrote it: fix it in
        place and commit again, or reset_graph when it was written from an older version.

        Args:
            dry_run: Only check and report what committing would do; nothing changes.
            replace: Node ids to run again as new instances even though the file does not change them.
        """
        run_id = ctx.open_run_id()
        replaced = tuple(replace or ())
        path = runtime.graph_file_status(run_id).path
        text = _read(path)
        key = (runtime.graph(run_id).version, _digest(text), replaced)
        if not dry_run and refused.get(run_id) == key:
            return (
                "not committed: this is the same file that was refused a moment ago, at the same graph "
                "version. Change it according to that error first, or do it another way (stop_node, "
                "a new node, reset_graph)."
            )
        result = await runtime.commit_graph_file(run_id, replace=replaced, dry_run=True)
        missing = [] if result.error else _undescribed(result, text)
        if missing:
            if not dry_run:
                refused[run_id] = key
            return (
                f"not committed: new and replaced nodes need a description saying what they do; empty for: "
                f"{', '.join(missing)}. The file was not changed; add the descriptions and commit again."
            )
        if not dry_run and result.error is None and result.changed:
            result = await runtime.commit_graph_file(run_id, replace=replaced)
        report = result.describe()
        if result.error is not None:
            if not dry_run:
                refused[run_id] = key
            if result.error_kind == "stale":
                return f"{report}\nThe file was not changed. Call reset_graph, read the file again and redo the edit."
            return f"{report}\nThe file was left exactly as you wrote it; fix it in place and commit again."
        refused.pop(run_id, None)
        if result.committed:
            report += f"\nThe graph file was rewritten at version {result.version}; read it again before the next edit."
        return report

    @tool
    async def reset_graph() -> str:
        """Drop every uncommitted edit: the current run's graph file is written again from the running graph.

        Use it after a commit was refused because the file was written from an older graph version,
        then read the file again and redo the edit.
        """
        run_id = ctx.open_run_id()
        status = await runtime.reset_graph_file(run_id)
        refused.pop(run_id, None)
        return f"{ctx.relative(status.path)} now holds graph version {status.current_version}, with no uncommitted edits."

    @tool
    async def show_graph() -> str:
        """The current run's graph as text, with each node's state and outcome, and whether the graph
        file holds uncommitted edits. The graph file itself only describes structure."""
        run_id = ctx.held_run_id()
        report = await runtime.status(run_id)
        states = {node.node_id: node.state for node in report.nodes}
        outcomes = {node.node_id: node.outcome for node in report.nodes if node.outcome is not None}
        lines = [f"run {run_id}: {report.run.status}", to_text(runtime.graph(run_id), states, outcomes)]
        acts_on_environment = [node.node_id for node in report.nodes if node.acts_on_environment]
        if acts_on_environment:
            lines.append(f"act on the environment: {', '.join(acts_on_environment)}")
        status = report.graph_file
        if status.has_changes:
            stale = " written from an older version; reset_graph before editing again," if status.stale else ""
            lines.append(f"graph file {ctx.relative(status.path)}: uncommitted edits,{stale} commit_graph to apply them")
        else:
            lines.append(f"graph file {ctx.relative(status.path)}: same as the running graph")
        return "\n".join(lines)

    @tool
    def register_node_code(name: str, source: str) -> str:
        """Save Python code for a new node type (or a new version of one you generated) and register it.

        The file holds exactly one node class, registered under `name` (its class attribute `name`
        must equal it). It is saved as code/<name>/v<N>.py with the next version number; the version
        is chosen for you. A node acting on the environment must declare acts_on_environment = True. To run
        an existing node on the new code, set its "code" in graph.json to the version returned here and
        commit; instances on older versions keep theirs.

        Args:
            name: The node type name; letters, digits and underscores.
            source: The whole Python module.
        """
        if not _TYPE_NAME.match(name):
            raise ToolError(f"type name {name!r} must start with a letter and hold only letters, digits and underscores")
        version = _next_version(ctx, name)
        path = write_generated_code(ctx.session_dir, name, version, source)
        try:
            # A trial registration first, in a registry of its own, so a file declaring other names
            # never reaches the real catalog.
            entries = NodeRegistry().register_from_file(path, version)
            names = sorted(entry.name for entry in entries)
            if names != [name]:
                raise ToolError(f"the file must declare exactly one node type named {name!r}; it declares {names}")
            entry = next(entry for entry in ctx.registry.register_from_file(path, version) if entry.name == name)
        except (ToolError, RegistryError, GraphValidationError) as e:
            path.unlink(missing_ok=True)
            raise ToolError(f"not registered: {e}") from None
        except Exception as e:
            path.unlink(missing_ok=True)
            raise ToolError(f"not registered: {type(e).__name__}: {e}") from None
        ports = ", ".join(
            [f"in {port.name} ({port.type_tag})" for port in entry.inputs] + [f"out {port.name} ({port.type_tag})" for port in entry.outputs]
        )
        return (
            f"registered {name} version {version} from {ctx.relative(path)}: "
            f"outcomes {', '.join(entry.outcomes)}; {ports or 'no ports'}; "
            f"{'acts on the environment' if entry.acts_on_environment else 'does not act on the environment'}. In graph.json write \"type\": \"{name}\" "
            f"(and \"code\": \"{version}\" to move an existing node onto this version)."
        )

    return [commit_graph, reset_graph, show_graph, register_node_code]


def _undescribed(result: GraphCommitResult, text: str) -> list[str]:
    """New nodes and nodes getting a new instance whose description in the file is empty."""
    ids = [*result.diff.nodes.added, *(record.node for record in result.diff.nodes.replaced)]
    if not ids:
        return []
    try:
        nodes = loads_graph_spec(text).nodes
    except GraphValidationError:
        return []
    return [node_id for node_id in ids if node_id in nodes and not nodes[node_id].description.strip()]


def _next_version(ctx: ToolContext, name: str) -> str:
    """One above every version written or registered under `name`, so no version names two codes."""
    known = (*list_generated_versions(ctx.session_dir, name), *ctx.registry.versions(name))
    numbers = [int(v) for v in known if v.isdigit()]
    version = str(max(numbers, default=0) + 1)
    while code_path(ctx.session_dir, name, version).exists():
        version = str(int(version) + 1)
    return version


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
