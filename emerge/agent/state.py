"""Per-turn runtime state and budgets. Never sent to the model."""

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emerge.models.types import Usage


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Budget for one turn. None means unlimited."""

    max_iterations: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens_total: int | None = None
    max_tool_calls: int | None = None

    @classmethod
    def from_config(cls, limits: Any) -> "RunLimits":
        """Copy `config.AgentConfig.limits`; taken duck-typed so agent/ does not import config.py."""
        data = limits.model_dump() if hasattr(limits, "model_dump") else dict(limits)
        return cls(**{name: data.get(name) for name in cls.__dataclass_fields__})


@dataclass(slots=True)
class RunContext:
    """Created once per `run_turn()`, passed to every middleware and tool call of that turn."""

    run_id: str
    workspace: Path
    intent_counter: int = 0
    usage: Usage = field(default_factory=Usage)

    @classmethod
    def create(cls, workspace: Path, *, intent_counter: int = 0, run_id: str | None = None) -> "RunContext":
        return cls(run_id=run_id or uuid.uuid4().hex[:12], workspace=workspace, intent_counter=intent_counter)

    def record_usage(self, usage: Usage) -> None:
        self.usage = self.usage + usage
