"""Subscriptions, start edges and endpoint addressing.

A subscription is a data dependency: the target's input reads the source's output, newest value
first. A start edge is a control dependency: once every source on it has finished as required, the
target may start. Start edges carry no data and live on the target node instance.
"""

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from emerge.exceptions import GraphValidationError

# Written in a graph file where a start edge only needs the source done, with any outcome. No node
# type may declare an outcome by this name.
ANY_OUTCOME = "*"


class Subscription(BaseModel):
    """One input port fed by one output port.

    Endpoints are (node id, port name), never positions, so a graph stays valid when a node is
    rebuilt with new ports in a different order. Cycles are allowed: feedback loops are normal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_node: str
    source_port: str
    target_node: str
    target_port: str


class StartSource(BaseModel):
    """One source on a start edge and what it must have finished with.

    `outcome=None` means "done", matching any outcome the source actually declared when it ended.
    A source that ended without an outcome (stopped, crashed) satisfies nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # References, like subscription endpoints: whether the node exists and declares the outcome is
    # for validation to report, with an issue code, not for the model to raise.
    node: str
    outcome: str | None = None

    def __str__(self) -> str:
        # "v[success]" asks for that outcome; a bare "v" for any.
        return self.node if self.outcome is None else f"{self.node}[{self.outcome}]"


class StartEdge(BaseModel):
    """An all-of group: the target starts once every source here has finished as required.

    A node with several start edges starts when any one of them fires, and only once. An edge with
    no sources can never fire; it is accepted here so that validation can report it by node instead
    of the model raising before the graph exists.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[StartSource, ...] = ()

    def __str__(self) -> str:
        return " and ".join(str(source) for source in self.sources) or "(no sources)"


def start_edges_from_after(after: Iterable[str]) -> tuple[StartEdge, ...]:
    """What `after=[...]` stands for: one start edge on which each listed node only has to be done.

    Nothing listed means no start edge at all, i.e. a root, never an edge without sources.
    """
    nodes = tuple(dict.fromkeys(after))
    return (StartEdge(sources=tuple(StartSource(node=node) for node in nodes)),) if nodes else ()


def parse_endpoint(text: str) -> tuple[str, str]:
    """Split "node.port". Ids and port names contain no dots, so any other shape is malformed."""
    node_id, dot, port = text.rpartition(".")
    if not dot or not node_id or not port:
        raise GraphValidationError(f"endpoint {text!r} is not of the form 'node.port'")
    return node_id, port
