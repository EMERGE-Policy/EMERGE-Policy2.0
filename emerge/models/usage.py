import json
import logging
from functools import cache
from typing import Any

from emerge.callbacks import EventEmitter
from emerge.events import Event, EventType
from emerge.models.types import ImagePart, Message, NativeItem, TextPart, ToolSpec, Usage

logger = logging.getLogger(__name__)

# Rough per-image costs (OpenAI tile pricing for a ~1024px image); only used for pre-request estimates.
IMAGE_TOKENS = {"low": 85, "auto": 765, "high": 765}
MESSAGE_OVERHEAD = 4


@cache
def _encoding() -> Any:
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except Exception:  # optional dependency; missing package or encoding download failure
        return None


def count_text_tokens(text: str) -> int:
    if not text:
        return 0
    encoding = _encoding()
    if encoding is not None:
        return len(encoding.encode(text, disallowed_special=()))
    # Fallback: ~4 ASCII characters per token, ~1 token per non-ASCII (e.g. CJK) character.
    ascii_chars = sum(1 for c in text if c.isascii())
    return ascii_chars // 4 + (len(text) - ascii_chars) + 1


def _native_tokens(item: NativeItem) -> int:
    data = dict(item.data)
    # Encrypted reasoning is opaque base64; its size only loosely tracks the reasoning tokens it carries.
    encrypted = data.pop("encrypted_content", None) or ""
    return count_text_tokens(json.dumps(data, ensure_ascii=False)) + len(encrypted) // 4


def estimate_message_tokens(message: Message) -> int:
    if message.native_items:
        return MESSAGE_OVERHEAD + sum(_native_tokens(n) for n in message.native_items)
    total = MESSAGE_OVERHEAD
    for part in message.content:
        if isinstance(part, TextPart):
            total += count_text_tokens(part.text)
        elif isinstance(part, ImagePart):
            total += IMAGE_TOKENS[part.detail or "auto"]
    for call in message.tool_calls:
        total += count_text_tokens(call.name) + count_text_tokens(json.dumps(call.arguments, ensure_ascii=False))
    return total


def estimate_tokens(
    messages: list[Message], tools: list[ToolSpec] | None = None, native_items: list[NativeItem] | None = None
) -> int:
    total = sum(estimate_message_tokens(m) for m in messages)
    total += sum(count_text_tokens(json.dumps(t.model_dump(), ensure_ascii=False)) for t in tools or [])
    total += sum(_native_tokens(n) for n in native_items or [])
    return total


class UsageTracker:
    """Accumulates usage in this process and emits one model.usage event per response for the main process to total."""

    def __init__(self, callbacks: EventEmitter | None = None) -> None:
        self.callbacks = callbacks
        self.total = Usage()
        self.requests = 0

    def record(self, usage: Usage, **payload: Any) -> None:
        self.total = self.total + usage
        self.requests += 1
        if self.callbacks is not None:
            self.callbacks.emit_nowait(Event(type=EventType.MODEL_USAGE, payload={**payload, "usage": usage.model_dump()}))
