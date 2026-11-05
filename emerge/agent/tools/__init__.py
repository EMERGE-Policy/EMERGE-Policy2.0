"""The main agent's tools. Each module builds its tools around one `ToolContext`.

Tools are registered in code (agent.py), never configured in JSON. A tool reports a failure the
model can act on by raising `ToolError` (or any exception): `Tool.invoke` turns it into an error
result instead of breaking the turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from emerge.exceptions import ToolError
from emerge.runtime.run import ENDED_STATUSES

if TYPE_CHECKING:
    from emerge.agent.memory import MemoryStore
    from emerge.agent.session import Session
    from emerge.agent.skills import SkillStore
    from emerge.config import WorkspaceConfig
    from emerge.graph.registry import NodeRegistry
    from emerge.runtime.service import RuntimeService
    from emerge.storage.base import Storage


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What the tools of one session act on."""

    # The conversation's folder: the root of every path the tools take and hand out.
    session_dir: Path
    config: WorkspaceConfig
    storage: Storage
    runtime: RuntimeService
    registry: NodeRegistry
    session: Session
    memory: MemoryStore
    skills: SkillStore

    def run_id(self) -> str:
        """The session's current run, which graph and execution tools act on."""
        run_id = self.session.current_run_id
        if run_id is None:
            raise ToolError("there is no run yet; start_run creates one")
        return run_id

    def relative(self, path: str | Path) -> str:
        """`path` as the model should write it: relative to the conversation's folder."""
        try:
            return Path(path).resolve().relative_to(self.session_dir.resolve()).as_posix()
        except ValueError:
            return str(path)

    def held_run_id(self) -> str:
        """The current run, which the runtime still holds, ended or not; for looking at it."""
        run_id = self.run_id()
        if any(run.run_id == run_id for run in self.runtime.runs()):
            return run_id
        raise ToolError(f"run {run_id} has ended and is no longer held; start_run begins a new one")

    def open_run_id(self) -> str:
        """The current run, which must not have ended; for changing it."""
        run_id = self.held_run_id()
        status = next(run.status for run in self.runtime.runs() if run.run_id == run_id)
        if status in ENDED_STATUSES:
            raise ToolError(f"run {run_id} has ended ({status}); start_run begins a new one")
        return run_id
