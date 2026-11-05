"""The storage interface: what is saved, and the shapes it comes back in.

One storage is one conversation's database, kept in the conversation's own folder
(`sessions/<session_id>/.emerge/state.db`): the session, its messages, its runs and their state,
and the index of its memory. Nothing in it belongs to another conversation.

Storage decides where things go and how they are read back; when a write happens belongs to the
runtime and the main agent. Everything here is a coroutine, because the one implementation talks to
SQLite through aiosqlite and the callers are already async.

Two rules the interface is built around:

- The conversation is append-only. A row is written when one message is complete and never
  rewritten. Compaction appends a summary and the turns it kept, and marks the rows before them; a
  turn cancelled inside the session marks its rows discarded. Neither deletes anything.
- Memory content is a file (`MEMORY.md` plus `asset/`). The database holds a search index of it,
  not a copy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from emerge.graph.graph import Graph
from emerge.graph.ports import MainAgentMessage
from emerge.graph.ports import Message as PortMessage
from emerge.models.types import Message, Usage

SessionStatus = Literal["idle", "turn_open"]
RunStatus = Literal["running", "idle", "paused", "closed", "finished", "stopped"]
NodeStateName = Literal[
    "pending", "starting", "running", "paused", "stopping", "finished", "failed", "stopped", "skipped"
]
AskStatus = Literal["pending", "answered", "void"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionRecord(_Model):
    id: str
    title: str = ""
    status: SessionStatus = "idle"
    # The system text generated when the session started, replaced when compaction rewrites the
    # prefix. Reopening reuses it as it is: the workspace may have changed while it was closed, and
    # regenerating would break the prefix that native items (Anthropic thinking) were signed against.
    system: str | None = None
    current_run_id: str | None = None
    intent_count: int = Field(default=0, ge=0)
    usage: Usage = Usage()
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    updated_at: float


class StoredMessage(_Model):
    """One row of the conversation. `seq` orders the messages."""

    seq: int = Field(ge=1)
    turn: int = Field(ge=0)
    message: Message
    # The turn that replaced earlier turns with a summary.
    summary: bool = False
    created_at: float


class PendingInput(_Model):
    seq: int = Field(ge=1)
    message: Message
    received_at: float


class MemoryHit(_Model):
    position: int = Field(ge=0)
    text: str


class MemoryAsset(_Model):
    """An image copied into the memory's `asset/`, registered by its path in the conversation folder."""

    path: str
    digest: str
    description: str = ""
    # Where the image came from: a node artifact, a reference, a pasted path.
    source: str = ""
    added_at: float


class RunRecord(_Model):
    id: str
    status: RunStatus = "running"
    # The timeline number most recently handed out; see `Storage.next_content_time`.
    content_time: int = Field(default=0, ge=0)
    # Digest of the `graph.json` the runtime last wrote for this run.
    graph_file_digest: str | None = None
    # The whole run is paused: nodes started meanwhile start paused. Kept apart from `status`, which
    # turns `closed` when the session closes, so a reopened run comes back paused.
    paused: bool = False
    started_at: float
    finished_at: float | None = None


class NodeState(_Model):
    """One node instance's lifecycle, kept per instance so a replaced node's record survives."""

    run_id: str
    node_id: str
    instance_id: str
    state: NodeStateName
    # The outcome the node returned, if it declared one; a node that ended without one has none.
    outcome: str | None = None
    reason: str = ""
    error: str | None = None
    updated_at: float


class PortRecord(_Model):
    run_id: str
    node: str
    port: str
    version: int = Field(ge=0)
    message: PortMessage
    stored_at: float


class QueuedNodeMessage(_Model):
    """A main-agent message for a node instance that had not started when it was sent."""

    run_id: str
    node_id: str
    instance_id: str
    # MainAgentMessage.seq, numbered per instance.
    seq: int = Field(ge=1)
    message: MainAgentMessage
    delivered: bool = False
    queued_at: float
    delivered_at: float | None = None


class AskRecord(_Model):
    id: str
    run_id: str
    node_id: str
    instance_id: str
    question: str
    status: AskStatus = "pending"
    answer: str = ""
    created_at: float
    answered_at: float | None = None


class RunState(_Model):
    """Everything needed to continue a run after the session that started it was closed."""

    run: RunRecord
    graph: Graph | None = None
    node_states: tuple[NodeState, ...] = ()
    # The messages each node instance still has to hand out, per input port, oldest first.
    port_messages: tuple[PortRecord, ...] = ()
    pending_asks: tuple[AskRecord, ...] = ()
    # Main-agent messages still waiting for their instance to start, by instance and seq.
    queued_messages: tuple[QueuedNodeMessage, ...] = ()


class SessionSummary(_Model):
    """A conversation as a list of conversations shows it, read without opening its storage."""

    session: SessionRecord
    # Oldest first.
    runs: tuple[RunRecord, ...] = ()


class Storage(ABC):
    """One conversation: its session record, its messages, its memory index, and its runs.

    Nothing here enforces order: the callers know which write belongs to which moment.
    """

    # The conversation's folder; the database lives under it.
    session_dir: Path

    # -- the session --------------------------------------------------------

    @abstractmethod
    async def create_session(
        self,
        session_id: str,
        *,
        title: str = "",
        system: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        """Write the session record of a new storage. A storage holds one; a second is refused."""

    @abstractmethod
    async def get_session(self) -> SessionRecord | None:
        """The session record, None before `create_session`."""

    @abstractmethod
    async def update_session(
        self,
        *,
        title: str | None = None,
        status: SessionStatus | None = None,
        current_run_id: str | None = None,
        clear_current_run: bool = False,
        intent_count: int | None = None,
        usage: Usage | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        """Change the fields given and stamp `updated_at`; `None` leaves a field alone.

        `None` cannot mean "clear" here, so the current run is cleared with `clear_current_run`.
        """

    @abstractmethod
    async def set_system_snapshot(self, system: str) -> SessionRecord:
        """Replace the system text the session sends; called on compaction, not on reopen."""

    # -- conversation -------------------------------------------------------

    @abstractmethod
    async def append_message(self, turn: int, message: Message, *, summary: bool = False) -> StoredMessage: ...

    @abstractmethod
    async def compact_messages(
        self, summary: Message, kept: Sequence[tuple[int, Message]]
    ) -> tuple[StoredMessage, ...]:
        """Append the compacted view and retire what it replaces, in one transaction.

        Every readable row is marked compacted; `kept` holds the turns kept after compaction as
        (turn, message), already stripped of thinking blocks. The summary row is appended first and
        marked as a summary. Returns the appended rows.
        """

    @abstractmethod
    async def discard_turn(self, turn: int) -> int:
        """Mark one turn's rows discarded and return how many. The rows stay in the database."""

    @abstractmethod
    async def load_messages(self) -> tuple[StoredMessage, ...]:
        """The conversation from the latest summary onward, in sequence order.

        Without a summary, the whole conversation. Rows marked compacted or discarded are not
        returned.
        """

    # -- queued user messages -----------------------------------------------

    @abstractmethod
    async def add_pending_input(self, message: Message) -> PendingInput: ...

    @abstractmethod
    async def pending_inputs(self) -> tuple[PendingInput, ...]:
        """Oldest first."""

    @abstractmethod
    async def remove_pending_input(self, seq: int) -> None:
        """Delete one queued message, by the `seq` it was given; called once it reached the agent."""

    # -- memory index -------------------------------------------------------

    @abstractmethod
    async def replace_memory_entries(self, entries: Sequence[str], file_digest: str) -> None:
        """Replace the index with `entries`, in file order, and record the digest indexed.

        One transaction: a reader sees either the old entries or the new ones.
        """

    @abstractmethod
    async def search_memory(self, query: str, *, limit: int = 20) -> tuple[MemoryHit, ...]:
        """Full-text hits, best first.

        Chinese is indexed character by character, so a query of any length finds a substring;
        Latin words are matched whole, case-insensitively.
        """

    @abstractmethod
    async def memory_file_digest(self) -> str | None:
        """Digest of the `MEMORY.md` content the index was last built from, None if never indexed."""

    @abstractmethod
    async def add_memory_asset(
        self,
        path: str,
        digest: str,
        *,
        description: str = "",
        source: str = "",
    ) -> MemoryAsset: ...

    @abstractmethod
    async def memory_assets(self) -> tuple[MemoryAsset, ...]: ...

    @abstractmethod
    async def remove_memory_assets(self, paths: Sequence[str]) -> int: ...

    # -- runs ---------------------------------------------------------------

    @abstractmethod
    async def create_run(self, *, graph_file_digest: str | None = None) -> RunRecord:
        """A new run, which becomes the session's current run."""

    @abstractmethod
    async def set_run_status(
        self, run_id: str, status: RunStatus, *, finished_at: float | None = None
    ) -> RunRecord: ...

    @abstractmethod
    async def get_run(self, run_id: str) -> RunRecord | None: ...

    @abstractmethod
    async def runs(self) -> tuple[RunRecord, ...]:
        """Every run of the session, oldest first."""

    @abstractmethod
    async def set_graph_file_digest(self, run_id: str, digest: str) -> RunRecord:
        """Record the digest of the `graph.json` last written for this run."""

    @abstractmethod
    async def set_run_paused(self, run_id: str, paused: bool) -> RunRecord:
        """Record whether the whole run is paused."""

    # -- graph --------------------------------------------------------------

    @abstractmethod
    async def save_graph(self, run_id: str, graph: Graph) -> None:
        """Overwrite the run's graph. Only the current version is kept; older ones are in the trace."""

    @abstractmethod
    async def load_graph(self, run_id: str) -> Graph | None: ...

    # -- nodes --------------------------------------------------------------

    @abstractmethod
    async def set_node_state(
        self,
        run_id: str,
        node_id: str,
        instance_id: str,
        state: NodeStateName,
        *,
        outcome: str | None = None,
        reason: str = "",
        error: str | None = None,
    ) -> NodeState: ...

    @abstractmethod
    async def node_states(self, run_id: str) -> tuple[NodeState, ...]: ...

    @abstractmethod
    async def set_node_snapshot(self, run_id: str, node_id: str, instance_id: str, data: Any) -> None:
        """Overwrite the node's status snapshot; only the newest one is kept."""

    @abstractmethod
    async def node_snapshot(self, run_id: str, node_id: str, instance_id: str) -> Any | None: ...

    # -- port messages ------------------------------------------------------

    @abstractmethod
    async def put_port_message(self, message: Message, *, run_id: str) -> PortRecord:
        """Store one published message and drop the oldest if the port is at its limit.

        `message.source_node` and `message.source_port` decide which port it belongs to.
        """

    @abstractmethod
    async def port_messages(
        self, run_id: str, node: str, port: str, after_version: int | None = None
    ) -> tuple[PortRecord, ...]:
        """The messages of one output port still kept, by version ascending.

        `after_version` returns only the ones published after that version, which is how a consumer
        continues from the last one it saw.
        """

    @abstractmethod
    async def latest_port_messages(self, run_id: str) -> tuple[PortRecord, ...]:
        """The newest message of every port, oldest first by version."""

    # -- main-agent messages to nodes not started yet --------------------------

    @abstractmethod
    async def queue_node_message(
        self, run_id: str, node_id: str, instance_id: str, message: MainAgentMessage
    ) -> QueuedNodeMessage:
        """Keep a message for an instance that has not started; `message.seq` is its number."""

    @abstractmethod
    async def queued_node_messages(
        self, run_id: str, *, instance_id: str | None = None, include_delivered: bool = False
    ) -> tuple[QueuedNodeMessage, ...]:
        """Queued messages of the run, or of one instance, by instance and seq."""

    @abstractmethod
    async def mark_node_messages_delivered(self, run_id: str, instance_id: str) -> int:
        """Mark every undelivered message of the instance delivered; returns how many."""

    # -- asks ---------------------------------------------------------------

    @abstractmethod
    async def add_ask(self, run_id: str, node_id: str, instance_id: str, question: str) -> AskRecord: ...

    @abstractmethod
    async def answer_ask(self, ask_id: str, answer: str) -> AskRecord: ...

    @abstractmethod
    async def void_ask(self, ask_id: str) -> AskRecord: ...

    @abstractmethod
    async def pending_asks(self, run_id: str) -> tuple[AskRecord, ...]: ...

    # -- timeline -----------------------------------------------------------

    @abstractmethod
    async def next_content_time(self, run_id: str) -> int:
        """Hand out the run's next timeline number, in the transaction that uses it.

        Stored per run rather than read back as a maximum: stamped values that never reach the
        database would be missed, and a number handed out twice would make two different moments
        look like one.
        """

    # -- resuming -----------------------------------------------------------

    @abstractmethod
    async def load_run_state(self, run_id: str) -> RunState | None:
        """Everything needed to continue a run: graph, node states, port messages, asks, timeline,
        and the messages still queued for instances that have not started."""

    @abstractmethod
    async def aclose(self) -> None: ...


async def open_storage(session_dir: Path | str, *, port_buffer: int = 64) -> Storage:
    """Open or create the database of the conversation in `session_dir` and return the one implementation."""
    from emerge.storage.sqlite import SqliteStorage

    return await SqliteStorage.open(Path(session_dir), port_buffer=port_buffer)


async def read_session(session_dir: Path | str) -> SessionSummary | None:
    """The conversation in `session_dir`, read without opening it for writing; None if there is none."""
    from emerge.storage.sqlite import read_session_summary

    return await read_session_summary(Path(session_dir))
