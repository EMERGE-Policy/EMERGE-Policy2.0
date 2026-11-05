"""What an action and a command have in common: each is asked for, carried out, and ends with a result.

    pending  ->  running  ->  completed | failed

An action is asked of one arm by a node; a command is asked of the simulation by a front end. The
simulation starts both and ends both with a result: one readable sentence (for an action, the driver's).
Whether it failed follows from the sentence (`status_of`).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, fields
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Self

from robot.base.errors import RobotError


class Status(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


UNFINISHED = frozenset({Status.PENDING, Status.RUNNING})

# How a result sentence says it failed, as in 1.0.
_FAILURE_OPENINGS = ("error:", "failed:", "unknown action")


def status_of(result: str) -> Status:
    """FAILED for a result sentence that reports a failure, COMPLETED for any other."""
    sentence = result.strip().lower()
    if sentence.startswith(_FAILURE_OPENINGS) or " failed" in sentence:
        return Status.FAILED
    return Status.COMPLETED


def new_id() -> str:
    """A short opaque id for one action or one command."""
    return uuid.uuid4().hex[:12]


def now() -> str:
    """UTC wall-clock time, e.g. 2026-11-02T10:00:01.123Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Request:
    """Mixed into `Action` and `Command`, dataclasses that declare these fields themselves:

        id, status, started_at, result, finished_at, duration_ms

    Gives both the same way to start and to end, and the same JSON form: every field that is set, in the
    order the class declares them. A status read from a file becomes a `Status`.
    """

    id: str | None
    status: Status | None
    started_at: str | None
    result: str | None
    finished_at: str | None
    duration_ms: int | None

    def __post_init__(self) -> None:
        if self.status is not None:
            try:
                self.status = Status(self.status)
            except ValueError:
                raise RobotError(f"{type(self).__name__} {self.id}: unknown status {self.status!r}") from None

    @property
    def unfinished(self) -> bool:
        """Whether it is still pending or running."""
        return self.status in UNFINISHED

    def start(self) -> None:
        self.status = Status.RUNNING
        self.started_at = now()

    def end(self, result: str, duration_ms: int | None = None) -> None:
        """End it with `result`; the status follows from the sentence."""
        self.status = status_of(result)
        self.result = result
        self.finished_at = now()
        self.duration_ms = duration_ms

    @classmethod
    def from_json(cls, value: Any) -> Self:
        if not isinstance(value, dict):
            raise RobotError(f"a {cls.__name__.lower()} must be a JSON object, got {value!r}")
        known = {item.name for item in fields(cls)}
        try:
            return cls(**{key: item for key, item in value.items() if key in known})
        except TypeError as e:
            raise RobotError(f"not a valid {cls.__name__.lower()}: {value!r} ({e})") from None

    def to_json(self) -> dict[str, Any]:
        return {key: item for key, item in asdict(self).items() if item is not None}
