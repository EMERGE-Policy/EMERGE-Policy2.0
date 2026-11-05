"""ACTION.json: for every arm, who holds it and the actions it was sent.

    {
      "left_arm": {
        "holder": {"run": "...", "node": "..."} | null,
        "actions": [{"id", "run", "action_type", "parameters", "step", "status", ...}, ...]
      },
      ...
    }

Nodes take and release arms and add actions; the simulation starts and ends the actions. Both change the
file only through `ActionFile.edit`, under its lock.

An arm's actions are kept oldest first, and the last one is its **current** action. The **holder** of an
arm is the node of a run that has it to itself: while it holds the arm and the arm has no action, the
simulation waits for it. A holder of another run than the one being served counts as nobody.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robot.base.errors import RobotError
from robot.base.protocol.fileio import edit_json, read_json
from robot.base.protocol.lifecycle import Request, Status
from robot.base.protocol.paths import action_path


@dataclass(frozen=True)
class Holder:
    """The node of a run that holds an arm."""

    run: str
    node: str

    @classmethod
    def from_json(cls, value: Any) -> Holder | None:
        if value is None:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("run"), str) or not isinstance(value.get("node"), str):
            raise RobotError(f"not a holder: {value!r}")
        return cls(value["run"], value["node"])

    def to_json(self) -> dict[str, str]:
        return {"run": self.run, "node": self.node}


@dataclass
class Action(Request):
    """One action asked of one arm, and how it went.

    The node that sends it gives `run`, `action_type`, `parameters`, and `step`: the tick of the snapshot
    it decided on. The simulation fills in the rest as the action starts and ends.
    """

    id: str
    run: str
    action_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    step: int = 0
    status: Status = Status.PENDING
    started_at: str | None = None
    result: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None


@dataclass
class Arm:
    """One arm: who holds it, and every action it was sent, oldest first."""

    holder: Holder | None = None
    actions: list[Action] = field(default_factory=list)

    @property
    def current(self) -> Action | None:
        """The last action sent: the one the arm is doing, or the one it did last. None before its first."""
        return self.actions[-1] if self.actions else None

    @classmethod
    def from_json(cls, value: Any) -> Arm:
        if not isinstance(value, dict):
            raise RobotError(f"not an arm: {value!r}")
        return cls(
            holder=Holder.from_json(value.get("holder")),
            actions=[Action.from_json(action) for action in value.get("actions") or []],
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "holder": self.holder.to_json() if self.holder else None,
            "actions": [action.to_json() for action in self.actions],
        }


@dataclass
class ActionFile:
    """The whole of ACTION.json: every arm, by name, in the order the simulation lists them."""

    arms: dict[str, Arm] = field(default_factory=dict)

    def arm(self, name: str) -> Arm:
        """The arm called `name`. Raises when the simulation has listed no arm yet, or not this one."""
        if not self.arms:
            raise RobotError("ACTION.json lists no arms yet; is the simulation running?")
        if name not in self.arms:
            raise RobotError(f"no arm {name!r}; the arms are {list(self.arms)}")
        return self.arms[name]

    def unfinished(self, status: Status | None = None) -> dict[str, Action]:
        """Every arm's current action that has not ended, by arm; given `status`, only those in it."""
        found: dict[str, Action] = {}
        for name, arm in self.arms.items():
            action = arm.current
            if action is None or not action.unfinished:
                continue
            if status is None or action.status == status:
                found[name] = action
        return found

    @classmethod
    def from_json(cls, value: Any) -> ActionFile:
        if not isinstance(value, dict):
            raise RobotError(f"ACTION.json must hold a JSON object, got {type(value).__name__}")
        return cls({name: Arm.from_json(arm) for name, arm in value.items()})

    def to_json(self) -> dict[str, Any]:
        return {name: arm.to_json() for name, arm in self.arms.items()}

    @classmethod
    def read(cls, folder: Path) -> ActionFile:
        """ACTION.json as it is now; no file reads as no arms."""
        return cls.from_json(read_json(action_path(folder)) or {})

    @classmethod
    @contextmanager
    def edit(cls, folder: Path) -> Iterator[ActionFile]:
        """Change ACTION.json under its lock. Nothing is written when the body raises."""
        with edit_json(action_path(folder)) as value:
            action_file = cls.from_json(value)
            yield action_file
            value.clear()
            value.update(action_file.to_json())
