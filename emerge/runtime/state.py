"""Node lifecycle states, the transitions between them, and one instance's runtime record.

State is kept per instance: a replaced node keeps its logical id, and the record of the instance it
replaced stays as it ended instead of being overwritten by the new one.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import get_args

from pydantic import BaseModel, ConfigDict, Field

from emerge.exceptions import RuntimeControlError
from emerge.graph import patch
from emerge.storage.base import NodeStateName


class LifecycleState(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"
    # Ended without starting: none of the node's start edges can fire any more.
    SKIPPED = "skipped"


# Taken from graph/patch.py rather than written again, so the patch rules and the runtime cannot
# disagree on what a name means; a name missing from LifecycleState fails at import.
TERMINAL_STATES = frozenset(LifecycleState(name) for name in patch.TERMINAL_STATES)
REPLACEABLE_STATES = frozenset(LifecycleState(name) for name in patch.REPLACEABLE_STATES)
if set(get_args(NodeStateName)) != set(LifecycleState):
    raise RuntimeError("storage.base.NodeStateName and runtime.state.LifecycleState list different states")

_S = LifecycleState
_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    _S.PENDING: frozenset({_S.STARTING, _S.SKIPPED}),
    # FAILED from STARTING: the code could not be loaded, or setup() failed.
    _S.STARTING: frozenset({_S.RUNNING, _S.PAUSED, _S.STOPPING, _S.FAILED, _S.STOPPED}),
    _S.RUNNING: frozenset({_S.PAUSED, _S.STOPPING, _S.FINISHED, _S.FAILED, _S.STOPPED}),
    _S.PAUSED: frozenset({_S.RUNNING, _S.STOPPING, _S.FINISHED, _S.FAILED, _S.STOPPED}),
    # A stop was requested, so the instance ends stopped however run() finishes, also when it has to
    # be ended by force or goes without a word.
    _S.STOPPING: frozenset({_S.STOPPED}),
    **{state: frozenset() for state in TERMINAL_STATES},
}


def can_transition(old: LifecycleState, new: LifecycleState) -> bool:
    return new in _TRANSITIONS[old]


def check_transition(old: LifecycleState, new: LifecycleState) -> None:
    if not can_transition(old, new):
        raise RuntimeControlError(f"illegal node state transition {old} -> {new}")


class NodeRuntimeState(BaseModel):
    """Where one node instance is in its lifecycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    instance_id: str
    state: LifecycleState = LifecycleState.PENDING
    pid: int | None = None
    # Only a finished instance has an outcome: the declared one its run() returned.
    outcome: str | None = None
    reason: str = ""
    # The most recent error, kept across later transitions until another one replaces it.
    error: str | None = None
    changed_at: float = Field(default_factory=time.time)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition(
        self,
        state: LifecycleState,
        *,
        pid: int | None = None,
        outcome: str | None = None,
        reason: str = "",
        error: str | None = None,
    ) -> NodeRuntimeState:
        """The record after moving to `state`; raises RuntimeControlError for an illegal move."""
        check_transition(self.state, state)
        if outcome is not None and state is not LifecycleState.FINISHED:
            raise RuntimeControlError(f"only a finished node has an outcome, not one in state {state}")
        return self.model_copy(
            update={
                "state": state,
                "pid": pid if pid is not None else self.pid,
                "outcome": outcome,
                "reason": reason if state is LifecycleState.FINISHED else "",
                "error": error if error is not None else self.error,
                "changed_at": time.time(),
            }
        )
