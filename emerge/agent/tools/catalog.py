"""What the main agent can build with: the registered node types, and the workspace skills."""

from __future__ import annotations

import json

from emerge.agent.tool import Tool, tool
from emerge.agent.tools import ToolContext
from emerge.exceptions import ToolError
from emerge.graph.registry import NodeEntry


def make_tools(ctx: ToolContext) -> list[Tool]:
    registry = ctx.registry

    @tool
    def list_node_types() -> str:
        """List every node type you can place in a graph: what it does, how it can end, its ports.

        Look a type up with describe_node_type before relying on its parameters or outcomes.
        """
        entries = registry.catalog()
        if not entries:
            return "No node types are registered."
        return "\n".join(_line(entry) for entry in entries)

    @tool
    def describe_node_type(name: str, version: str | None = None) -> str:
        """Everything about one node type: ports and their type tags, the parameter schema, the
        outcomes it declares, whether it acts on the environment, what it reads directly, and where its
        code comes from.

        Args:
            name: The node type name, as list_node_types shows it.
            version: A generated type's code version; leave out for the current one.
        """
        entry = registry.find_entry(name, version)
        if entry is None:
            known = ", ".join(registry.names()) or "none"
            raise ToolError(f"no node type {name!r}{f' version {version!r}' if version else ''}; registered: {known}")
        data = entry.to_dict()
        versions = registry.versions(name)
        if versions:
            data["generated_versions"] = list(versions)
        return json.dumps(data, ensure_ascii=False, indent=2)

    tools = [list_node_types, describe_node_type]
    skills = ctx.skills.list()
    if skills:
        listing = "; ".join(f"{skill.name}: {skill.description}" for skill in skills)

        @tool(
            description=(
                "Load a skill: a written procedure for a situation. Load one when its description "
                f"matches what you are about to do, then follow it. Available: {listing}"
            )
        )
        def use_skill(name: str) -> str:
            """Args:
            name: The skill's name.
            """
            skill = ctx.skills.get(name)
            if skill is None:
                raise ToolError(f"no skill {name!r}; available: {', '.join(s.name for s in skills)}")
            return f"Skill {skill.name}:\n\n{skill.body}"

        tools.append(use_skill)
    return tools


def _line(entry: NodeEntry) -> str:
    parts = [f"{entry.name}: {entry.description or '(no description)'}"]
    parts.append(f"outcomes: {', '.join(entry.outcomes)}")
    if entry.inputs:
        parts.append("inputs: " + ", ".join(f"{port.name} ({port.type_tag})" for port in entry.inputs))
    if entry.outputs:
        parts.append("outputs: " + ", ".join(f"{port.name} ({port.type_tag})" for port in entry.outputs))
    if entry.acts_on_environment:
        parts.append("ACTS ON THE ENVIRONMENT")
    if entry.version is not None:
        parts.append(f"generated code, version {entry.version}")
    return "- " + "; ".join(parts)
