"""A scripted fake provider for driving AgentLoop without any real API."""
import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from emerge.models.base import ModelProvider, events_of_response
from emerge.models.client import ModelClient
from emerge.models.types import (
    Message, ModelRequest, ModelResponse, NativeItem, StreamEvent, TextPart, ToolCall, Usage,
)
from emerge.agent.messages import SUMMARY_PREFIX


def reply(text: str = "", calls: list[tuple[str, str, dict]] = (), usage: tuple[int, int] = (100, 10), native=None) -> ModelResponse:
    msg = Message(
        role="assistant",
        content=[TextPart(text=text)] if text else [],
        tool_calls=[ToolCall(id=i, name=n, arguments=a) for i, n, a in calls],
        native_items=native or [],
    )
    return ModelResponse(message=msg, usage=Usage(input_tokens=usage[0], output_tokens=usage[1], total_tokens=sum(usage)))


class FakeProvider(ModelProvider):
    provider = "fake"
    api_mode = "responses"

    def __init__(self, script: list[Any] | None = None, summary: Any = "SUMMARY TEXT", delay: float = 0.0) -> None:
        self.script = list(script or [])
        self.summary = summary
        self.delay = delay
        self.requests: list[ModelRequest] = []
        self.summary_requests: list[ModelRequest] = []

    def _answer(self, request: ModelRequest) -> ModelResponse:
        first = request.messages[0] if request.messages else None
        if first is not None and first.role == "user" and "Conversation to summarize:" in first.text:
            self.summary_requests.append(request)
            s = self.summary
            if isinstance(s, BaseException) or (isinstance(s, type) and issubclass(s, BaseException)):
                raise s
            return reply(s, usage=(50, 5))
        self.requests.append(request)
        if not self.script:
            return reply("done (script empty)")
        item = self.script.pop(0)
        if callable(item):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        return item

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self.delay:
            await asyncio.sleep(self.delay)
        response = self._answer(request)
        for event in events_of_response(response):
            yield event

    async def _generate_once(self, request: ModelRequest) -> ModelResponse:
        return self._answer(request)

    def _refuses_streaming(self, error: Exception) -> bool:
        return False

    async def probe_api_compatibility(self) -> None:
        pass


class ListEmitter:
    """Minimal EventEmitter: collects events."""
    def __init__(self):
        self.events = []
    async def emit(self, event):
        self.events.append(event)
    def emit_nowait(self, event):
        self.events.append(event)
    def with_event_defaults(self, **kw):
        return self


def client_for(provider: FakeProvider, callbacks=None) -> ModelClient:
    return ModelClient(provider, model="fake-model", callbacks=callbacks)


def dump_history(history) -> list[str]:
    out = []
    for i, turn in enumerate(history.turns):
        parts = []
        for m in turn.messages:
            if m.role == "assistant":
                parts.append(f"A({m.text!r}{', calls=' + str([c.name for c in m.tool_calls]) if m.tool_calls else ''})")
            elif m.role == "tool":
                parts.append(f"T[{m.tool_call_id}]({m.text[:40]!r})")
            else:
                parts.append(f"{m.role[0].upper()}({m.text[:40]!r})")
        out.append(f"turn{i}{'(summary)' if turn.summary else ''}: " + " ".join(parts))
    return out
