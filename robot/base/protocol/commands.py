"""command.json: the one command a front end gave the simulation, and how it went.

    {"id", "op", "status", "started_at", "result", "finished_at", "duration_ms"}

A front end puts a command there (`frontend.command_port`); the simulation carries it out between ticks
and ends it. One command at a time: a new one takes the place of the last only once that one has ended.
Before the first command there is no file, which reads as a command with nothing set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path

from robot.base.protocol.fileio import edit_json, read_json
from robot.base.protocol.lifecycle import Request, Status
from robot.base.protocol.paths import command_path

# What a command can ask for: its `op`.
RESET = "reset"  # put the scene back to the conversation's first scene
OPS = (RESET,)


@dataclass
class Command(Request):
    """The command in command.json; every field None before the first."""

    id: str | None = None
    op: str | None = None
    status: Status | None = None
    started_at: str | None = None
    result: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None

    def replace_with(self, command_id: str, op: str) -> None:
        """Become a new command asking for `op`, pending; what was here before is dropped."""
        for item in fields(self):
            setattr(self, item.name, None)
        self.id, self.op, self.status = command_id, op, Status.PENDING

    @classmethod
    def read(cls, folder: Path) -> Command:
        """command.json as it is now."""
        return cls.from_json(read_json(command_path(folder)) or {})

    @classmethod
    @contextmanager
    def edit(cls, folder: Path) -> Iterator[Command]:
        """Change command.json under its lock. Nothing is written when the body raises."""
        with edit_json(command_path(folder)) as value:
            command = cls.from_json(value)
            yield command
            value.clear()
            value.update(command.to_json())
