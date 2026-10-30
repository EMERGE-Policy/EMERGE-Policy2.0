"""How the supervisor ends node processes.

A node that ended is never started again (decided 2026-10-22): it stays ended, and redoing work is
the main agent's call. Stopping is therefore the only policy here.
"""

from __future__ import annotations

from dataclasses import dataclass

from emerge.config import RuntimeConfig


@dataclass(frozen=True, slots=True)
class StopPolicy:
    # Seconds between the stop request and ending the process by force. A node that never reaches a
    # blocking call or `wait_for_run_permission()` is killed after this and recorded as stopped, with
    # the forced end in its error. A well-written node gets there in time: one that does not is a bug
    # in the node, and the framework does not design around it.
    stop_timeout: float

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> StopPolicy:
        return cls(stop_timeout=config.stop_timeout)
