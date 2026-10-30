"""Reading and editing workspace files the way a coding agent does.

The main agent draws and changes the graph by editing `runs/<run_id>/graph.json` with these tools
and then committing it; generated node code and MEMORY.md can be edited the same way. Editing a
file changes nothing by itself: the graph file waits for `commit_graph`, code for
`register_node_code`, and memory is re-indexed the next time it is searched.

Paths are relative to the workspace (or absolute inside it). After symlinks are followed a path
must still be inside the workspace. `config.json` holds the API keys and can be neither read nor
written; the database under `.emerge/` and other sessions' directories are off limits too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from emerge.agent.tool import Tool, ToolResult, tool
from emerge.agent.tools import ToolContext
from emerge.exceptions import ToolError
from emerge.models.types import ToolCall

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
DEFAULT_LIMIT = 2000
MAX_LINE = 2000
# Tools here that can write a graph file, which the stale-call check covers.
WRITING_TOOLS = frozenset({"edit_file", "write_file"})


def resolve_path(workspace: Path, path: str, *, session_id: str) -> Path:
    """The file `path` names, refused when it leads outside what the main agent may touch."""
    root = workspace.resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ToolError(f"{path} is outside the workspace {root}")
    relative = resolved.relative_to(root)
    if relative == Path("config.json"):
        raise ToolError("config.json holds the API keys; it can be neither read nor written")
    if relative.parts[:1] == (".emerge",):
        raise ToolError(".emerge/ holds the runtime's database; it is not for editing")
    if relative.parts[:1] == ("sessions",) and len(relative.parts) > 1 and relative.parts[1] != session_id:
        raise ToolError("that belongs to another session")
    return resolved


def is_graph_file(workspace: Path, path: Any) -> bool:
    """Whether `path` is a run's graph file, `runs/<run_id>/graph.json`."""
    if not isinstance(path, str):
        return False
    root = workspace.resolve()
    candidate = Path(path).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_relative_to(root):
        return False
    parts = resolved.relative_to(root).parts
    return len(parts) == 3 and parts[0] == "runs" and parts[2] == "graph.json"


def writes_graph_file(workspace: Path, call: ToolCall) -> bool:
    return call.name in WRITING_TOOLS and is_graph_file(workspace, call.arguments.get("path"))


def make_tools(ctx: ToolContext) -> list[Tool]:
    workspace = ctx.workspace

    def resolve(path: str) -> Path:
        return resolve_path(workspace, path, session_id=ctx.session.id)

    def shown(path: Path) -> str:
        return path.relative_to(workspace.resolve()).as_posix()

    @tool
    def read_file(path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
        """Read a workspace file. Text comes back with line numbers; an image comes back as the image.

        Line numbers are only for reading: they are not part of the file, so never put them into
        edit_file's old_text. Read long files in parts with offset and limit.

        Args:
            path: Relative to the workspace, e.g. runs/<run_id>/graph.json.
            offset: First line to show, counting from 1.
            limit: How many lines to show (2000 when left out).
        """
        target = resolve(path)
        if not target.is_file():
            raise ToolError(f"{path} does not exist" if not target.exists() else f"{path} is a directory")
        if target.suffix.lower() in IMAGE_SUFFIXES:
            return ToolResult(content=f"Image: {shown(target)}", images=[shown(target)])
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            raise ToolError(f"{path} is not a text file") from None
        if not lines:
            return ToolResult(content=f"{shown(target)} is empty")
        start = max(1, offset or 1)
        if start > len(lines):
            raise ToolError(f"{path} has {len(lines)} lines; offset {start} is past the end")
        count = limit if limit is not None and limit > 0 else DEFAULT_LIMIT
        chosen = lines[start - 1 : start - 1 + count]
        body = "\n".join(
            f"{number:6d}\t{line if len(line) <= MAX_LINE else line[:MAX_LINE] + ' ...(line cut)'}"
            for number, line in enumerate(chosen, start=start)
        )
        end = start + len(chosen) - 1
        more = f"; {len(lines) - end} more after line {end}" if end < len(lines) else ""
        return ToolResult(content=f"{shown(target)}, lines {start}-{end} of {len(lines)}{more}:\n{body}")

    @tool
    def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replace exact text in a workspace file.

        old_text must appear in the file exactly once, character for character (whitespace included,
        no line numbers); with replace_all every occurrence is replaced. Read the file first: after a
        commit the graph file is rewritten and what you read before may be out of date.

        Args:
            path: Relative to the workspace.
            old_text: The text to replace, copied from the file.
            new_text: What replaces it.
            replace_all: Replace every occurrence instead of requiring exactly one.
        """
        target = resolve(path)
        if not target.is_file():
            raise ToolError(f"{path} does not exist; write_file creates a file")
        if not old_text:
            raise ToolError("old_text is empty; use write_file to write a whole file")
        if old_text == new_text:
            raise ToolError("old_text and new_text are the same; nothing would change")
        text = target.read_text(encoding="utf-8")
        found = text.count(old_text)
        if found == 0:
            raise ToolError(f"old_text does not appear in {path}; read the file again and copy the text exactly")
        if found > 1 and not replace_all:
            raise ToolError(
                f"old_text appears {found} times in {path}; include more surrounding text to make it unique, "
                "or set replace_all"
            )
        target.write_text(text.replace(old_text, new_text), encoding="utf-8")
        return f"Edited {shown(target)}: replaced {found if replace_all else 1} occurrence(s). {_effect(target, workspace)}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write a whole workspace file, creating it (and its folders) or replacing what it held.

        Prefer edit_file for small changes to an existing file.

        Args:
            path: Relative to the workspace.
            content: The complete new content.
        """
        target = resolve(path)
        if target.is_dir():
            raise ToolError(f"{path} is a directory")
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        verb = "Replaced" if existed else "Created"
        return f"{verb} {shown(target)} ({len(content.splitlines())} lines). {_effect(target, workspace)}"

    return [read_file, edit_file, write_file]


def _effect(target: Path, workspace: Path) -> str:
    """What has to happen next for the edit to take effect."""
    parts = target.relative_to(workspace.resolve()).parts
    if len(parts) == 3 and parts[0] == "runs" and parts[2] == "graph.json":
        return "The graph file is a draft: nothing changes in the run until commit_graph."
    if parts[:1] == ("code",):
        return "Code takes effect only once registered with register_node_code."
    if target.name == "MEMORY.md":
        return "Memory searches see it right away; the system prompt picks it up after the next compaction or session."
    return "Writing a file has no effect on the run by itself."
