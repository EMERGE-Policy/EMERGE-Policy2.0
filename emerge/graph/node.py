"""The node base class and the interface a node sees at run time.

A node declares its ports and its lifecycle in code; the graph only ever stores its registered
name. `NodeContext` is the whole surface an implementation gets: the runtime provides it in the
node process (phase 6), the graph only fixes its shape here.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from emerge.exceptions import NodeError
from emerge.graph.edge import ANY_OUTCOME
from emerge.graph.ports import NAME_PATTERN, MainAgentMessage, Message, Port, Ref

if TYPE_CHECKING:
    from pathlib import Path

    from emerge.config import WorkspaceConfig
    from emerge.events import Event
    from emerge.models.client import ModelClient

_NAME_RE = re.compile(NAME_PATTERN)


class Outcome(BaseModel):
    """How a node finished: what it returned from `run()`, and one line saying why.

    The name must be one the node type declared in `Node.outcomes`. A node that returns None, or a
    name it never declared, has ended without an outcome: the runtime records that it ended, and
    no start edge can fire from it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    reason: str = ""

    def __init__(self, name: str | None = None, reason: str | None = None, /, **data: Any) -> None:
        # Positional too, so a node can end with `return Outcome("success", "cup placed")`. Pydantic
        # validates through this method with keyword data, which passes through untouched; a field
        # given both ways is a TypeError, as for any other call.
        positional = {"name": name, "reason": reason}
        super().__init__(**{key: value for key, value in positional.items() if value is not None}, **data)


@runtime_checkable
class NodeContext(Protocol):
    """What a node may call. Signatures are fixed here; the implementation lands in runtime/.

    Anything that sends or receives a message is a coroutine, so a node only has to remember one
    rule: communication is awaited. `await` means the message was handed to this process's
    transport, not that any subscriber has read it; messages from one node are stamped in call order.
    """

    node_id: str
    # This process's incarnation of `node_id`; a replacement runs under a new one. Execution stamps
    # it on every message, reference and event the node produces.
    instance_id: str
    run_id: str
    workspace: Path
    config: WorkspaceConfig
    # runs/<run_id>/nodes/<node_id>/<instance_id>/
    artifacts_dir: Path
    # The one line the main agent wrote about this instance when it placed it.
    instance_description: str
    # The outcomes this node's type declared; the node cannot change them.
    outcomes: tuple[str, ...]

    async def get_latest_input_message(self, port: str) -> Message | None:
        """Newest message on a subscribed input, or None if nothing arrived yet."""
        ...

    async def wait_for_input_message(
        self,
        port: str,
        newer_than: int | None = None,
        timeout: float | None = None,
        where: Callable[[Message], bool] | None = None,
    ) -> Message:
        """Wait for a message on `port` with a version above `newer_than` (or the first one).

        With `where`, the first such message it accepts: messages the port still keeps are checked
        first (all of them when `newer_than` is None), then new ones as they arrive. `where` runs in
        the node process, one message at a time; the framework never interprets what it checks.
        Raises WaitTimeout when the timeout expires, InputClosed when the input has closed and holds
        no such message, and StopRequested when a stop is requested while blocked. If the input is
        rewired meanwhile, the wait goes on over the new source with what it publishes after the
        rewiring; what it had published before is not handed out. Each message names its source
        (`source_node`, `source_port`, `source_instance`); no separate notice of the change is sent.
        """
        ...

    async def wait_for_input_messages(
        self,
        ports: Sequence[str],
        *,
        newer_than: Mapping[str, int] | None = None,
        match: Callable[[Message], Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Message]:
        """Wait until each of `ports` has a message above its cursor and return one per port.

        `newer_than` holds a version cursor per port; ports it leaves out take any message. With
        `match`, the messages returned are a group whose extracted values are all equal, e.g. the
        same key on every port, and the earliest such group when there are several; the node can
        continue from the returned versions. `match` runs in the node process and its values are
        never interpreted. Raises WaitTimeout and StopRequested like `wait_for_input_message`, and
        InputClosed once an input has closed and the group can no longer be completed.
        """
        ...

    def iter_input_messages(self, port: str) -> AsyncIterator[Message]:
        """Every message on `port` after the one already consumed, in version order.

        Ends normally only when the input closes, after handing out the messages still left on it.
        Raises StopRequested when a stop is requested while blocked, like every other wait. A rewired
        input does not end it: the iteration goes on over the new source, with what that source
        publishes from the rewiring on, and the messages it had published before are skipped.
        Messages can be displaced by newer ones, so a gap here does not mean production was skipped.
        """
        ...

    async def publish_output_message(
        self,
        port: str,
        *,
        text: str | None = None,
        data: Mapping[str, Any] | None = None,
        refs: Iterable[Ref] = (),
    ) -> Message:
        """Publish to an output port; execution assigns the version and stamps the message."""
        ...

    async def report_to_main_agent(self, text: str) -> None:
        """Send a progress note to the main agent."""
        ...

    async def emit_event(self, event: Event | dict[str, Any]) -> None:
        """Push a custom event (progress notes and the like) to the main process."""
        ...

    async def ask_main_agent(self, question: str) -> str:
        """Ask the main agent a question and wait for its answer."""
        ...

    # The two calls below share one queue of messages the main agent sent to this instance; each
    # message is handed out once. Delivery only: reading them, and acting on them, is the node's call.

    async def take_main_agent_messages(self) -> tuple[MainAgentMessage, ...]:
        """Messages from the main agent not taken yet, oldest first; empty when there are none.

        Does not wait for new ones, so a node can check at its own boundaries, e.g. in an agent
        loop's checkpoint hook.
        """
        ...

    async def wait_for_main_agent_message(self, timeout: float | None = None) -> MainAgentMessage:
        """The next message from the main agent not taken yet.

        Raises WaitTimeout when the timeout expires and StopRequested when a stop is requested
        while blocked.
        """
        ...

    async def set_status_snapshot(self, data: Mapping[str, Any]) -> None:
        """Replace this node's small "what I am doing now" structure; only the latest one is kept.

        Nothing is pushed to anyone: the main agent reads it when it looks at the node.
        """
        ...

    def ref_dir(self, ref: Ref) -> Path:
        """The directory holding the content `ref` points at.

        It is runs/<run_id>/nodes/<producer>/<producer_instance>/<port>/<key>/; whether it holds one
        file or many is the node's choice. A node builds a ref for content it is about to write from
        its own id, `instance_id`, the output port it publishes on and a key of its choosing, then
        asks for the directory instead of assembling a path itself.
        """
        ...

    async def wait_for_run_permission(self) -> bool:
        """Cooperative checkpoint: blocks while paused, returns False when a stop is requested.

        An instance started while paused (its whole run is paused, say) blocks here from the first
        call. Once a stop has been requested the instance ends as stopped however `run()` finishes: an
        outcome returned after that is only logged, never recorded, and fires no start edge.
        """
        ...

    def get_model_client(self) -> ModelClient:
        """Create the model client on first use; nodes that never call a model pay nothing."""
        ...


class Node:
    """Subclass this and declare ports, outcomes and lifecycle.

    `run()` returning means the node is done; looping inside it means it keeps running. There is no
    separate mode field for the two. The constructor is for cheap assignment only: I/O belongs in
    `setup()`.
    """

    name: str = ""
    description: str = ""
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    irreversible: bool = False
    # How this kind of node can finish, e.g. ("success", "failure", "unclear"). Every registered
    # type declares its own; the base class only leaves the slot. A node ends with `run()` returning
    # one of these, and start edges elsewhere can ask for a specific one.
    outcomes: tuple[str, ...] = ()
    # Live sources the node reads directly (files written outside the framework): name -> note.
    # Declared so the graph is not blind to inputs; they take no part in wiring or scheduling.
    reads: tuple[tuple[str, str], ...] = ()
    Params: ClassVar[type[BaseModel] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Declarations are checked as soon as the class exists, so a mistake is reported where it is
        # made. An empty `name` or `outcomes` passes here, for a shared base class that is never
        # placed in a graph; registration refuses both.
        # Port names may repeat within inputs or outputs; preserve every declaration.
        cls.inputs = tuple(cls.inputs)
        cls.outputs = tuple(cls.outputs)
        cls.reads = tuple(cls.reads)
        cls.outcomes = _checked_outcomes(cls)
        _require_unique(cls.name, "read", [name for name, _ in cls.reads])

    def __init__(self, params: BaseModel | None = None) -> None:
        self.params = params

    async def setup(self, ctx: NodeContext) -> None:
        pass

    async def run(self, ctx: NodeContext) -> Outcome | None:
        raise NotImplementedError

    async def teardown(self, ctx: NodeContext) -> None:
        pass

    def __repr__(self) -> str:
        return f"Node({self.name!r})"


def _checked_outcomes(cls: type[Node]) -> tuple[str, ...]:
    # ("done") without the comma is a string, and would pass as four one-letter outcomes.
    if isinstance(cls.outcomes, str):
        raise NodeError(f"node {cls.name!r}: outcomes must be a tuple of names, e.g. ({cls.outcomes!r},)")
    outcomes = tuple(cls.outcomes)
    _require_unique(cls.name, "outcome", list(outcomes))
    for outcome in outcomes:
        if not isinstance(outcome, str) or not _NAME_RE.match(outcome):
            raise NodeError(f"node {cls.name!r}: outcome {outcome!r} is not a valid name")
        if outcome == ANY_OUTCOME:
            raise NodeError(f"node {cls.name!r}: {ANY_OUTCOME!r} means any outcome in a graph file and cannot be declared")
    return outcomes


def _require_unique(node_name: str, kind: str, names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise NodeError(f"node {node_name!r}: duplicate {kind} name {name!r}")
        seen.add(name)
