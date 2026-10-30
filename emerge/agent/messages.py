"""Conversation history grouped into turns.

A turn is one exchange: the input that started it, the assistant messages, the tool calls and
their results. Turns are the unit of compaction: a turn is kept or dropped as a whole, and the
history is only ever appended to between compactions.

A `HistoryListener` hears every change right after it is made, so a caller can keep a copy of the
conversation elsewhere (the main agent writes it to storage) without the history knowing where.
"""

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from emerge.models.types import Message, NativeItem

SUMMARY_PREFIX = "[Earlier conversation, compacted]\n\n"

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryListener(Protocol):
    """Told of each change to a History, in order. Called synchronously, so it must not block."""

    def turn_started(self, turn: int, message: Message) -> None: ...

    def messages_appended(self, turn: int, messages: Sequence[Message]) -> None: ...

    def turns_dropped(self, start: int, stop: int) -> None:
        """Turns `start` up to (not including) `stop` were dropped from the end."""

    def compacted(self, summary: Message, kept: Sequence[tuple[int, Message]]) -> None:
        """Compaction replaced the history: `summary` is turn 0, `kept` the messages of the turns after it."""


class Turn(_Model):
    messages: list[Message] = Field(default_factory=list)
    # Set on the single turn that replaces earlier turns after compaction.
    summary: bool = False


class History(_Model):
    turns: list[Turn] = Field(default_factory=list)
    _listener: HistoryListener | None = PrivateAttr(default=None)

    def __len__(self) -> int:
        return len(self.turns)

    def set_listener(self, listener: HistoryListener | None) -> None:
        self._listener = listener

    def start_turn(self, message: Message) -> None:
        self.turns.append(Turn(messages=[message]))
        if self._listener is not None:
            self._listener.turn_started(len(self.turns) - 1, message)

    def append_messages_to_current_turn(self, *messages: Message) -> None:
        """Append to the open (last) turn; creates one if the history is empty."""
        if not self.turns:
            self.turns.append(Turn())
        self.turns[-1].messages.extend(messages)
        if self._listener is not None and messages:
            self._listener.messages_appended(len(self.turns) - 1, messages)

    def flatten_messages(self) -> list[Message]:
        return [message for turn in self.turns for message in turn.messages]

    def native_items(self) -> list[NativeItem]:
        # Native items travel attached to the assistant messages that produced them.
        return [item for message in self.flatten_messages() for item in message.native_items]

    def truncate_to_turn_count(self, count: int) -> None:
        """Drop turns added at the end, keeping the first `count`; used to roll back an unfinished turn."""
        stop = len(self.turns)
        del self.turns[count:]
        if self._listener is not None and count < stop:
            self._listener.turns_dropped(count, stop)

    def compact(self, count: int, summary: str) -> None:
        """Replace the first `count` turns with one summary turn and strip thinking from every turn.

        The listener hears it once, with the result: the kept turns as they are sent from now on.
        """
        self.replace_oldest_turns_with_summary(count, summary)
        self.strip_thinking()
        if self._listener is not None:
            kept = [(index, message) for index, turn in enumerate(self.turns) if index > 0 for message in turn.messages]
            self._listener.compacted(self.turns[0].messages[0], kept)

    # The two steps of `compact`. Neither tells the listener: it hears the finished compaction.

    def replace_oldest_turns_with_summary(self, count: int, summary: str) -> None:
        """Replace the first `count` turns with one summary turn. Compaction only."""
        if not 0 < count <= len(self.turns):
            raise ValueError(f"cannot replace {count} of {len(self.turns)} turns")
        text = SUMMARY_PREFIX + summary.strip()
        self.turns = [Turn(messages=[Message.user(text)], summary=True), *self.turns[count:]]

    def strip_thinking(self) -> None:
        """Drop Anthropic thinking blocks from every turn; run after compaction changed the prefix."""
        for index, turn in enumerate(self.turns):
            messages = [strip_thinking(message) for message in turn.messages]
            if any(new is not old for new, old in zip(messages, turn.messages)):
                self.turns[index] = turn.model_copy(update={"messages": messages})


def strip_thinking(message: Message) -> Message:
    """Return the message without thinking / redacted_thinking native items.

    Anthropic validates that a thinking block's signature matches everything before it, so those
    blocks cannot be replayed once compaction rewrites the prefix. Other native items are kept.
    """
    if message.role != "assistant" or not message.native_items:
        return message
    items = [
        item
        for item in message.native_items
        if not (item.provider == "anthropic" and item.data.get("type") in _THINKING_TYPES)
    ]
    if len(items) == len(message.native_items):
        return message
    return message.model_copy(update={"native_items": items})
