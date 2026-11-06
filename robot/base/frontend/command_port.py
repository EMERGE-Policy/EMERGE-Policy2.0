"""The command port: how a front end tells the simulation serving a conversation what to do.

A command goes into command.json; the simulation carries it out between ticks and writes how it went.
One command at a time. Nodes do not use this.

    reset    put the scene back to the conversation's first scene
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from robot.base.errors import CommandBusy
from robot.base.protocol.commands import OPS, Command
from robot.base.protocol.lifecycle import new_id
from robot.base.protocol.paths import robot_dir


def send_command(session_dir: Path | str, op: str) -> str:
    """Ask the simulation serving this conversation to carry out `op`. Returns the command id at once.

    Raises CommandBusy while the previous command has not ended.
    """
    if op not in OPS:
        raise ValueError(f"unknown command {op!r}; the commands are {list(OPS)}")
    command_id = new_id()
    with Command.edit(robot_dir(session_dir)) as command:
        if command.unfinished:
            raise CommandBusy(f"command {command.id} ({command.op}) has not ended")
        command.replace_with(command_id, op)
    return command_id


def read_command(session_dir: Path | str, command_id: str) -> dict[str, Any] | None:
    """The command with this id, with its status and result; None when it is not the current one."""
    command = Command.read(robot_dir(session_dir))
    return command.to_json() if command.id == command_id else None
