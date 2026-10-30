import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Self


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"

    NODE_STATE = "node.state"
    NODE_REPORT = "node.report"
    NODE_CUSTOM = "node.custom"
    NODE_ASK = "node.ask"
    NODE_ANSWER = "node.answer"
    # The main agent sent a node a message (MainAgentMessage); recorded so the trace shows it.
    NODE_MAIN_AGENT_MESSAGE = "node.main_agent_message"

    PORT_PUBLISHED = "port.published"

    MODEL_STARTED = "model.started"
    MODEL_TOKEN = "model.token"
    MODEL_FINISHED = "model.finished"
    MODEL_USAGE = "model.usage"

    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"

    GRAPH_PATCHED = "graph.patched"

    AGENT_MESSAGE = "agent.message"


@dataclass(slots=True)
class Event:
    type: str
    timestamp: float = field(default_factory=time.time)
    run_id: str | None = None
    node_id: str | None = None
    # The node instance that produced the event. A replaced node keeps its id, so this is what lets
    # the runtime drop a late event from the instance that was replaced.
    instance_id: str | None = None
    # Position on the run's graph-wide timeline (see graph.ports.Message), stamped by the bus on what
    # nodes and the main agent say to each other; None on process events such as model tokens.
    content_time: int | None = None
    irreversible: bool | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = str(self.type)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**data)
