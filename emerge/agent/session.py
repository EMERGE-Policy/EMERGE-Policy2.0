"""A conversation with the main agent: written down as it happens, closed and opened again later.

This serves one case: the user leaves in the middle and later opens the same session to go on. A
run paused before leaving is the same case. Everything needed is stored when it happens:

- each message once it is complete (user messages, model replies once they arrived whole, tool
  results, runtime info, compaction), through the History listener and one queue of writes, so
  rows land in the order things happened; a reply still streaming is not in the history, so it is
  never written;
- each user message as soon as it arrives (`pending_inputs`), removed once it is in the
  conversation;
- the system text the session sends, replaced only when compaction rebuilds it.

`Session.close()`: the main agent finishes as `AgentLoop.close_turn()` describes (a reply still
streaming is dropped, a running tool finishes, calls not run get "not executed: session closed"),
node questions being answered on the side are cancelled, every run of the session still open is
closed (`RuntimeService.close_run`), and the queued writes are written. The session stays
`turn_open` if a turn was in progress, `idle` otherwise.

`Session.open()`: the conversation is read back turn by turn and the stored system text is used as
it is: the workspace files may have changed meanwhile, and rebuilding it would change the prefix
the Anthropic thinking blocks were signed against (a 400). Each run left `closed` is carried on
(`RuntimeService.resume_run`) and its report goes to the main agent: appended to the unfinished
turn, which then continues, or, if the session was idle, with the next runtime info. Queued user
messages are handed over as if they had just arrived.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

from emerge.agent.messages import History, Turn
from emerge.exceptions import StorageError
from emerge.models.types import Message, Usage
from emerge.runtime.run import ENDED_STATUSES
from emerge.storage.base import PendingInput, SessionRecord, SessionStatus, Storage, StoredMessage

if TYPE_CHECKING:
    from emerge.agent.agent import MainAgent

logger = logging.getLogger(__name__)

Write = Callable[[], Awaitable[Any]]


class Session:
    """One conversation: its record, its history, its runs, and the queue its writes go through.

    Create one with `Session.create(agent)` or open a stored one with `Session.open(agent, id)`;
    the agent serves it until `close()`.
    """

    def __init__(self, storage: Storage, record: SessionRecord, history: History) -> None:
        self.storage = storage
        self.record = record
        self.history = history
        self.status: SessionStatus = record.status
        # The run graph and execution tools act on; one at a time (Storage.create_run sets it).
        self.current_run_id = record.current_run_id
        self.agent: MainAgent | None = None
        # Evaluated when a compaction is written, after the system text was rebuilt.
        self.system_source: Callable[[], str] | None = None
        self.closed = False
        self._writes: asyncio.Queue[Write] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        # id(message) -> pending_inputs seq, for user messages not yet in the conversation.
        self._pending: dict[int, int] = {}

    @property
    def id(self) -> str:
        return self.record.id

    # -- opening and closing ------------------------------------------------------

    @classmethod
    async def create(cls, agent: MainAgent, *, title: str = "") -> Session:
        """A new conversation, served by `agent` from now on."""
        record = await agent.storage.create_session(title=title)
        session = cls(agent.storage, record, History())
        await agent.attach(session)
        await agent.start()
        return session

    @classmethod
    async def open(cls, agent: MainAgent, session_id: str) -> Session:
        """Open a stored conversation and go on where it was left; closed runs carry on, unasked."""
        storage = agent.storage
        record = await storage.get_session(session_id)
        if record is None:
            raise StorageError(f"no session {session_id!r}")
        session = cls(storage, record, history_from_rows(await storage.load_messages(session_id)))
        await agent.attach(session)
        reports = [
            await agent.runtime.resume_run(run.id)
            for run in await storage.runs_of_session(session_id)
            if run.status == "closed"
        ]
        await agent.start(
            reports=reports,
            unfinished_turn=record.status == "turn_open",
            pending=await storage.pending_inputs(session_id),
        )
        return session

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        agent = self.agent
        if agent is not None:
            await agent.detach()
            for run in agent.runtime.runs():
                if run.session_id == self.id and run.status not in ENDED_STATUSES:
                    await agent.runtime.close_run(run.run_id)
        await self.flush()
        if self._writer is not None:
            self._writer.cancel()
            await asyncio.gather(self._writer, return_exceptions=True)
            self._writer = None

    @staticmethod
    async def list(storage: Storage, *, limit: int | None = None) -> tuple[SessionRecord, ...]:
        """Stored sessions, most recently updated first (what `/sessions` shows)."""
        return await storage.list_sessions(limit=limit)

    @staticmethod
    async def delete(storage: Storage, session_id: str) -> None:
        """Delete a closed session, with its conversation and its own memory; global memory stays."""
        await storage.delete_session(session_id)

    # -- what the conversation writes (HistoryListener) ---------------------------

    def turn_started(self, turn: int, message: Message) -> None:
        self._append(turn, message)

    def messages_appended(self, turn: int, messages: Sequence[Message]) -> None:
        for message in messages:
            self._append(turn, message)

    def turns_dropped(self, start: int, stop: int) -> None:
        for turn in range(start, stop):
            self._queue(partial(self.storage.discard_turn, self.id, turn))

    def compacted(self, summary: Message, kept: Sequence[tuple[int, Message]]) -> None:
        self._queue(partial(self.storage.compact_messages, self.id, summary, list(kept)))
        if self.system_source is not None:
            source = self.system_source
            self._queue(lambda: self.storage.set_system_snapshot(self.id, source()))

    # -- other writes -----------------------------------------------------------------

    async def add_pending(self, message: Message) -> PendingInput:
        """Keep a user message the moment it arrives; it is removed once it is in the conversation."""
        return await self.storage.add_pending_input(self.id, message)

    def hold_pending(self, message: Message, seq: int) -> None:
        """`message` came from `pending_inputs` row `seq`: delete that row once the message is written."""
        self._pending[id(message)] = seq

    def set_status(self, status: SessionStatus) -> None:
        if status != self.status:
            self.status = status
            self._queue(partial(self.storage.update_session, self.id, status=status))

    def set_intent(self, count: int) -> None:
        self._queue(partial(self.storage.update_session, self.id, intent_count=count))

    def set_usage(self, usage: Usage) -> None:
        self._queue(partial(self.storage.update_session, self.id, usage=usage))

    def save_system(self, system: str) -> None:
        self._queue(partial(self.storage.set_system_snapshot, self.id, system))

    async def flush(self) -> None:
        """Wait until every queued write is in the database."""
        if self._writer is not None:
            await self._writes.join()

    # -- the write queue --------------------------------------------------------------

    def _append(self, turn: int, message: Message) -> None:
        self._queue(partial(self.storage.append_message, self.id, turn, message))
        seq = self._pending.pop(id(message), None)
        if seq is not None:
            # After the message itself: the queue keeps order, so it is never in neither place.
            self._queue(partial(self.storage.remove_pending_input, self.id, seq))

    def _queue(self, write: Write) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_all(), name=f"session-writes:{self.id}")
        self._writes.put_nowait(write)

    async def _write_all(self) -> None:
        while True:
            write = await self._writes.get()
            try:
                await write()
            except Exception:
                logger.exception("session %s: a write to storage failed", self.id)
            finally:
                self._writes.task_done()


def history_from_rows(rows: Sequence[StoredMessage]) -> History:
    """Rebuild the history from `load_messages`: rows are in order, one turn per turn number."""
    history = History()
    turn: int | None = None
    for row in rows:
        if row.turn != turn:
            history.turns.append(Turn(summary=row.summary))
            turn = row.turn
        history.turns[-1].messages.append(row.message)
    return history
