import uuid
from collections.abc import AsyncIterator
from typing import Any

from emerge.callbacks import EventEmitter
from emerge.events import Event, EventType
from emerge.exceptions import ModelError
from emerge.models.base import ModelProvider
from emerge.models.retry import RateLimiter, RetryPolicy, retry_stream, with_retry
from emerge.models.types import ModelRequest, ModelResponse, ReasoningDelta, StreamDone, StreamEvent, TextDelta
from emerge.models.usage import UsageTracker, estimate_tokens


class ModelClient:
    def __init__(
        self,
        provider: ModelProvider,
        *,
        model: str = "",
        retry: RetryPolicy | None = None,
        limiter: RateLimiter | None = None,
        callbacks: EventEmitter | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.retry = retry or RetryPolicy()
        self.limiter = limiter or RateLimiter()
        self.callbacks = callbacks
        self.usage = UsageTracker(callbacks)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """The complete response. The request and the events are the same as `stream()`'s."""
        response: ModelResponse | None = None
        async for event in self._call(request, stream=False):
            if isinstance(event, StreamDone):
                response = event.response
        if response is None:
            raise ModelError("model stream ended without a response")
        return response

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        async for event in self._call(request, stream=True):
            yield event

    async def _call(self, request: ModelRequest, *, stream: bool) -> AsyncIterator[StreamEvent]:
        # Thinking and answer pieces go out as model.token events whichever method was called, so
        # whatever displays a call can show it as it happens. Once a piece is out, a failure is not
        # retried: the same text would be shown twice.
        call_id = self._start(request, stream=stream)
        try:
            async for event in retry_stream(lambda: self.provider.stream(request), self.retry, self.limiter):
                if isinstance(event, TextDelta):
                    self._emit(EventType.MODEL_TOKEN, call_id, kind="text", part=event.part, text=event.text)
                elif isinstance(event, ReasoningDelta):
                    self._emit(EventType.MODEL_TOKEN, call_id, kind="reasoning", part=event.part, text=event.text)
                elif isinstance(event, StreamDone):
                    self._finish(call_id, event.response)
                yield event
        except ModelError as e:
            self._emit(EventType.MODEL_FINISHED, call_id, error=str(e))
            raise

    async def probe_api_compatibility(self) -> None:
        await with_retry(self.provider.probe_api_compatibility, self.retry, self.limiter)

    def estimate_tokens(self, request: ModelRequest) -> int:
        return estimate_tokens(request.messages, request.tools, request.native_items)

    async def aclose(self) -> None:
        await self.provider.aclose()

    def _start(self, request: ModelRequest, stream: bool) -> str:
        call_id = uuid.uuid4().hex[:12]
        self._emit(EventType.MODEL_STARTED, call_id, stream=stream, messages=len(request.messages), tools=len(request.tools))
        return call_id

    def _finish(self, call_id: str, response: ModelResponse) -> None:
        self._emit(
            EventType.MODEL_FINISHED,
            call_id,
            response_id=response.response_id,
            finish_reason=response.finish_reason,
            tool_calls=[c.name for c in response.message.tool_calls],
            usage=response.usage.model_dump(),
            # False when the service refused to stream and the request went without streaming.
            streamed=self.provider.streaming,
        )
        self.usage.record(response.usage, **self._base(call_id))

    def _base(self, call_id: str) -> dict[str, Any]:
        return {"call_id": call_id, "provider": self.provider.provider, "api_mode": self.provider.api_mode, "model": self.model}

    def _emit(self, type: EventType, call_id: str, **payload: Any) -> None:
        if self.callbacks is not None:
            self.callbacks.emit_nowait(Event(type=type, payload=self._base(call_id) | payload))
