"""Running the graph: start a run, look at it, pause, resume, stop, end it, message a node.

A session has one current run at a time; every tool here acts on it. There is no tool that waits
for nodes: a node ending, a node that can never start, the graph going idle, node reports and
nodes' other state changes all arrive as messages of their own.
"""

from __future__ import annotations

import json
from typing import Any

from emerge.agent.tool import Tool, tool
from emerge.agent.tools import ToolContext
from emerge.exceptions import RuntimeControlError, ToolError
from emerge.graph.graph import Graph
from emerge.runtime.run import ENDED_STATUSES
from emerge.runtime.service import NodeView

# Tools here that change what runs, which the stale-call check covers.
GUARDED = frozenset({"start_run", "pause", "resume", "stop_node", "end_run", "send_to_node"})


def make_tools(ctx: ToolContext) -> list[Tool]:
    runtime = ctx.runtime

    @tool
    async def start_run() -> str:
        """Start a run with an empty graph. Then draw the graph in its file and commit_graph.

        A session has one run at a time: end the current one first (end_run) to start another.
        """
        current = ctx.session.current_run_id
        if current is not None and any(run.run_id == current and run.status not in ENDED_STATUSES for run in runtime.runs()):
            raise ToolError(f"run {current} is still open; end_run first, or keep drawing in it")
        run = await runtime.start_run(Graph(), ctx.session.id)
        ctx.session.current_run_id = run.run_id
        path = runtime.graph_file_status(run.run_id).path
        relative = ctx.relative(path)
        return (
            f"started run {run.run_id}; its graph is empty. Read {relative}, draw the nodes, inputs and "
            "start conditions in it, then commit_graph (dry_run first if unsure)."
        )

    @tool
    async def run_status() -> str:
        """The current run: its status, every node with its state, outcome and reason or error, the
        questions waiting for an answer, and whether the graph file holds uncommitted edits."""
        report = await runtime.status(ctx.held_run_id())
        run = report.run
        lines = [f"run {run.run_id}: {run.status}{' (whole run paused)' if run.paused else ''}, graph version {run.graph_version}"]
        lines += [f"- {_node_line(node)}" for node in report.nodes] or ["(no nodes yet)"]
        for ask in report.pending_asks:
            lines.append(f"question {ask.id} from {ask.node_id} (being answered on the side): {ask.question}")
        status = report.graph_file
        lines.append(
            f"graph file {ctx.relative(status.path)}: "
            + ("uncommitted edits (commit_graph applies them)" if status.has_changes else "same as the running graph")
        )
        return "\n".join(lines)

    @tool
    async def node_detail(node_id: str) -> str:
        """One node of the current run in detail: instance, state, outcome and reason, last error, its
        latest status snapshot, the newest message on each output, its artifact directory, earlier
        instances, its parameters and code version.

        Args:
            node_id: The node's id in the graph.
        """
        run_id = ctx.held_run_id()
        try:
            detail = await runtime.node_detail(run_id, node_id)
        except RuntimeControlError as e:
            raise ToolError(str(e)) from None
        data: dict[str, Any] = detail.model_dump(mode="json", exclude={"instances"})
        data["earlier_instances"] = [
            record.model_dump(mode="json", include={"instance_id", "state", "outcome", "reason", "error"})
            for record in detail.instances
            if record.instance_id != detail.node.instance_id
        ]
        return json.dumps(data, ensure_ascii=False, indent=2)

    @tool
    async def pause(node_id: str | None = None) -> str:
        """Pause one node, or the whole run: then running nodes pause at their next checkpoint and
        nodes that start meanwhile start paused.

        Args:
            node_id: The node to pause; leave out to pause the whole run.
        """
        return await _control(ctx, "pause", node_id)

    @tool
    async def resume(node_id: str | None = None) -> str:
        """Resume one paused node, or the whole run.

        Args:
            node_id: The node to resume; leave out to resume the whole run.
        """
        return await _control(ctx, "resume", node_id)

    @tool
    async def stop_node(node_id: str) -> str:
        """Stop one node. It ends as stopped and stays in the graph; no start edge fires from it.

        Args:
            node_id: The node to stop.
        """
        return await _control(ctx, "stop", node_id)

    @tool
    async def end_run(stopped: bool = False) -> str:
        """End the current run for good: nodes still running are stopped. Do it when the task is done.

        Args:
            stopped: True when abandoning the task rather than finishing it; the run then ends as stopped.
        """
        run_id = ctx.open_run_id()
        try:
            run = await (runtime.stop(run_id) if stopped else runtime.end_run(run_id))
        except RuntimeControlError as e:
            raise ToolError(str(e)) from None
        return f"run {run_id} ended as {run.status} at graph version {run.graph_version}"

    @tool
    async def send_to_node(node_id: str, text: str, data: dict[str, Any] | None = None) -> str:
        """Send a message to a node of the current run. What it does with it is up to the node; this
        only reports whether it was delivered.

        Args:
            node_id: The node to send to.
            text: The message.
            data: Optional small JSON data for the node.
        """
        run_id = ctx.open_run_id()
        try:
            delivery = await runtime.send_to_node(run_id, node_id, text=text, data=data)
        except RuntimeControlError as e:
            raise ToolError(f"not delivered: {e}") from None
        if delivery.queued:
            return f"queued for {node_id} ({delivery.instance_id}): it has not started yet and gets the message when it does"
        return f"delivered to {node_id} ({delivery.instance_id}) as message {delivery.seq}"

    return [start_run, run_status, node_detail, pause, resume, stop_node, end_run, send_to_node]


async def _control(ctx: ToolContext, action: str, node_id: str | None) -> str:
    run_id = ctx.open_run_id()
    runtime = ctx.runtime
    call = {"pause": runtime.pause, "resume": runtime.resume, "stop": runtime.stop}[action]
    try:
        result = await call(run_id, node_id)
    except RuntimeControlError as e:
        raise ToolError(str(e)) from None
    if node_id is None:
        return f"run {run_id} is now {result.status}"
    return f"{node_id} is now {result.state}"


def _node_line(node: NodeView) -> str:
    parts = [f"{node.node_id} ({node.type}, {node.instance_id}): {node.state}"]
    if node.outcome is not None:
        parts.append(f"outcome {node.outcome}" + (f" ({node.reason})" if node.reason else ""))
    if node.error:
        parts.append(f"error: {node.error}")
    if node.irreversible:
        parts.append("irreversible")
    if node.description:
        parts.append(f"about: {node.description}")
    return "; ".join(parts)
