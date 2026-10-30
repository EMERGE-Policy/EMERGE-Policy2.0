"""System prompt assembly, runtime-info injection, and the compaction that rewrites the prefix.

Everything here exists to keep the request prefix stable: the system text and the tool list are
built once and reused, history is only appended to, and compaction is the single operation that
changes what came before.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from emerge.agent.messages import History, Turn
from emerge.agent.state import RunContext
from emerge.exceptions import ModelError
from emerge.models.client import ModelClient
from emerge.models.types import Message, ModelRequest, ModelResponse, StreamDone, ToolSpec
from emerge.models.usage import estimate_tokens

Instruction = str | Callable[[], str]

COMPACT_PROMPT = Path(__file__).resolve().parent / "prompts" / "compact.md"


class ContextBuilder:
    """Combines ordered instruction fragments into the system text.

    The result is cached: it is built on first use and rebuilt only after compaction, when files
    such as MEMORY.md may have changed. `system` starts from a text built earlier instead, e.g. the
    one a reopened conversation was sent with, which stays in use until the next compaction.
    """

    def __init__(
        self,
        instructions: Sequence[Instruction] = (),
        *,
        runtime_info: Callable[[], str] | None = None,
        system: str | None = None,
    ) -> None:
        self.instructions = list(instructions)
        self.runtime_info = runtime_info
        self._system: str | None = system
        self._last_info: str | None = None

    def get_system_prompt(self) -> str:
        if self._system is None:
            self._system = self._build()
        return self._system

    def rebuild_system_prompt(self) -> str:
        # Compaction dropped the old runtime-info messages, so the next request appends the current one again.
        self._last_info = None
        self._system = self._build()
        return self._system

    def append_runtime_info(self, history: History) -> bool:
        """Append the runtime info as a message when it changed since the last append.

        It is written into the history so later requests replay it verbatim; previous appends are
        never removed, only compaction drops them.
        """
        if self.runtime_info is None:
            return False
        text = self.runtime_info().strip()
        if not text or text == self._last_info:
            return False
        self._last_info = text
        history.append_messages_to_current_turn(Message.user(text))
        return True

    def _build(self) -> str:
        parts: list[str] = []
        for item in self.instructions:
            text = (item() if callable(item) else item).strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts)


class RequestBuilder:
    """Builds the model request from the cached system text, history and fixed tool list.

    Prefix stability depends on this being the only place a request is assembled: the system text
    and tool specs are the same objects on every call, and the messages grow only at the end.
    """

    def __init__(self, context: ContextBuilder, history: History, tools: Sequence[ToolSpec] = ()) -> None:
        self.context = context
        self.history = history
        self.tools = list(tools)

    def build(self) -> ModelRequest:
        messages: list[Message] = []
        system = self.context.get_system_prompt()
        if system:
            messages.append(Message.system(system))
        messages.extend(self.history.flatten_messages())
        return ModelRequest(messages=messages, tools=list(self.tools))

    def rebuild(self, request: ModelRequest) -> ModelRequest:
        """Refresh messages and tools after compaction changed the history, keeping per-request overrides."""
        fresh = self.build()
        return request.model_copy(update={"messages": fresh.messages, "tools": fresh.tools})


class Compactor:
    """Replaces older turns with one summary so the request fits the context budget.

    Kept turns stay whole; Anthropic thinking blocks are removed from them because a thinking
    block's signature no longer matches once the text before it has been rewritten.
    """

    def __init__(
        self,
        client: ModelClient,
        context: ContextBuilder,
        *,
        max_context_tokens: int,
        compact_threshold: float = 0.8,
        keep_ratio: float = 0.5,
        prompt: str | None = None,
        stream: bool = True,
    ) -> None:
        self.client = client
        self.context = context
        self.max_context_tokens = max_context_tokens
        self.compact_threshold = compact_threshold
        self.keep_ratio = keep_ratio
        self.prompt = prompt if prompt is not None else COMPACT_PROMPT.read_text(encoding="utf-8")
        self.stream = stream
        # Exact count for the prefix as it stood at the last request: (messages sent, input tokens).
        self._measured: tuple[int, int] | None = None

    @property
    def budget(self) -> int:
        return int(self.max_context_tokens * self.compact_threshold)

    def over_budget(self, request: ModelRequest) -> bool:
        return self.estimate_input_tokens(request) > self.budget

    def estimate_input_tokens(self, request: ModelRequest) -> int:
        """Exact count for the last measured prefix, plus an estimate for what was appended since."""
        if self._measured is not None and len(request.messages) >= self._measured[0]:
            sent, tokens = self._measured
            tail = estimate_tokens(request.messages[sent:])
            return tokens + tail
        return estimate_tokens(request.messages, request.tools, request.native_items)

    def record_usage(self, message_count: int, input_tokens: int) -> None:
        """Store the exact count of the request that just returned.

        `message_count` is how many messages that request sent (the system message included), so
        later calls only estimate what was appended after it.
        """
        if input_tokens > 0:
            self._measured = (message_count, input_tokens)

    async def compact(self, history: History, run_context: RunContext | None = None) -> bool:
        """Summarize the oldest turns in place. Returns whether anything was compacted."""
        turns = history.turns
        if len(turns) <= 1:
            return False
        keep_budget = max(1, int(self.budget * self.keep_ratio))
        index = len(turns)
        kept = 0
        while index > 0:
            size = estimate_tokens(turns[index - 1].messages)
            if index < len(turns) and kept + size > keep_budget:
                break
            kept += size
            index -= 1
        if index == 0:
            index = 1
        if index == 1 and turns[0].summary:
            # Only the previous summary would be folded, which barely shrinks anything and would cost
            # a model call on every request while the newest turn alone exceeds the keep budget.
            return False
        summary = await self._summarize(turns[:index], run_context)
        if not summary:
            return False
        history.compact(index, summary)
        # The prefix was rewritten, so the exact count and the cached system text are both stale.
        self._measured = None
        self.context.rebuild_system_prompt()
        return True

    async def _summarize(self, turns: list[Turn], run_context: RunContext | None) -> str:
        text = transcript([message for turn in turns for message in turn.messages])
        request = ModelRequest(messages=[Message.user(self.prompt.replace("{conversation}", text))])
        response = await call_model(self.client, request, stream=self.stream)
        if run_context is not None:
            run_context.record_usage(response.usage)
        return response.message.text.strip()


async def call_model(client: ModelClient, request: ModelRequest, *, stream: bool = True) -> ModelResponse:
    """One model call. Both ways send the same streamed request and emit the same token events."""
    if not stream:
        return await client.generate(request)
    response: ModelResponse | None = None
    async for event in client.stream(request):
        if isinstance(event, StreamDone):
            response = event.response
    if response is None:
        raise ModelError("model stream ended without a response")
    return response


def transcript(messages: list[Message]) -> str:
    """Plain-text rendering of messages for the compaction prompt; images become their paths."""
    lines: list[str] = []
    for message in messages:
        if message.role == "system":
            continue
        text = message.text.strip()
        images = [f"[image: {part.path}]" for part in message.images]
        if message.role == "assistant":
            if text:
                lines.append(f"assistant: {text}")
            for call in message.tool_calls:
                arguments = json.dumps(call.arguments, ensure_ascii=False)
                lines.append(f"assistant calls {call.name}({arguments})")
        elif message.role == "tool":
            lines.append(f"tool result: {text}" if text else "tool result: (empty)")
        else:
            lines.append(f"user: {text}" if text else "user:")
        lines.extend(images)
    return "\n".join(lines)
