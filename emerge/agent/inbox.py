"""The main agent's inbox: what waits for it, in what order, and what wakes it.

Four kinds of items come in:

- `user`: a message from the user.
- `notice`: a run notification from `runtime.feedback`: a node ended (with its outcome and reason,
  or its state and error), a node can never start, the graph went idle, or a node's reports and
  its other state changes (starting, running, paused, stopping), merged per time window. New port
  messages and custom node events never come here.
- `qa`: the record of a node's question that was answered on the side (see agent.py), so that
  later steps agree with the answer.
- `control`: the record of a command the user gave through the control channel. The command itself
  was carried out at once by control.py; only the record waits here, so the model learns of it.

Priority is control, then user messages, then notifications and question records together. While
the agent sleeps, any item wakes it and starts a turn. While it works, items wait for the next
boundary where messages may be added (before a model call) and enter the running turn there all at
once, in priority order: the control records as one message, each user message as itself, and all
notifications and question records merged into one message, so a burst of notifications costs one
model call instead of one each.

The intent counter goes up by one for every user message and every control command, never for a
notification or a question record. The main agent refuses a graph or execution tool call the
model made before the counter last moved: better to stop one change too many (the user only asked
about progress) than to carry out one the user has just overruled.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from emerge.models.types import Message

InboxKind = Literal["control", "user", "notice", "qa"]

_PRIORITY: dict[str, int] = {"control": 0, "user": 1, "notice": 2, "qa": 2}


@dataclass(frozen=True, slots=True)
class InboxItem:
    kind: InboxKind
    text: str
    # The user's message as given, images included; None for the other kinds.
    message: Message | None = None
    run_id: str | None = None
    # The row in Storage.pending_inputs holding a user message until it reaches the conversation.
    pending_seq: int | None = None
    received_at: float = field(default_factory=time.time)


class Inbox:
    def __init__(self, intent: int = 0) -> None:
        self._items: list[InboxItem] = []
        self._intent = intent
        self._arrived = asyncio.Event()

    @property
    def intent(self) -> int:
        """User messages and control commands received so far in this session."""
        return self._intent

    def __len__(self) -> int:
        return len(self._items)

    def put_user(self, message: Message, *, pending_seq: int | None = None, counted: bool = True) -> InboxItem:
        """`counted=False` for a message counted already, e.g. one kept while the session was closed."""
        if counted:
            self._intent += 1
        return self._put(InboxItem("user", message.text, message=message, pending_seq=pending_seq))

    def put_control(self, text: str, *, run_id: str | None = None) -> InboxItem:
        self._intent += 1
        return self._put(InboxItem("control", text, run_id=run_id))

    def put_notice(self, text: str, *, run_id: str | None = None) -> InboxItem:
        return self._put(InboxItem("notice", text, run_id=run_id))

    def put_qa(self, text: str, *, run_id: str | None = None) -> InboxItem:
        return self._put(InboxItem("qa", text, run_id=run_id))

    def take(self) -> list[InboxItem]:
        """Everything waiting, highest priority first and in arrival order within a priority."""
        items = sorted(self._items, key=lambda item: _PRIORITY[item.kind])
        self._items.clear()
        self._arrived.clear()
        return items

    async def wait(self) -> None:
        """Sleep until something is waiting."""
        while not self._items:
            self._arrived.clear()
            await self._arrived.wait()

    def _put(self, item: InboxItem) -> InboxItem:
        self._items.append(item)
        self._arrived.set()
        return item


def to_messages(items: Sequence[InboxItem]) -> list[Message]:
    """The messages taken items enter the conversation as, in the order given (see `Inbox.take`)."""
    messages: list[Message] = []
    controls = [item for item in items if item.kind == "control"]
    if controls:
        lines = ["[Control channel] Done directly by the user, not through you. Do not undo it unless asked:"]
        lines += [_bullet(item.text) for item in controls]
        messages.append(Message.user("\n".join(lines)))
    messages += [item.message or Message.user(item.text) for item in items if item.kind == "user"]
    notices = [item for item in items if item.kind == "notice"]
    answers = [item for item in items if item.kind == "qa"]
    if notices or answers:
        lines = []
        if notices:
            runs = {item.run_id for item in notices}
            single = next(iter(runs)) if len(runs) == 1 else None
            lines.append(f"[Run notifications{f': run {single}' if single else ''}]")
            lines += [_bullet(item.text if single else f"run {item.run_id}: {item.text}") for item in notices]
        if answers:
            lines.append("[Node questions answered]")
            lines += [_bullet(item.text) for item in answers]
        messages.append(Message.user("\n".join(lines)))
    return messages


def _bullet(text: str) -> str:
    first, *rest = text.strip().splitlines() or [""]
    return "\n".join([f"- {first}", *(f"  {line}" for line in rest)])
