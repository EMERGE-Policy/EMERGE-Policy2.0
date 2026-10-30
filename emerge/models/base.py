import base64
import json
import logging
import mimetypes
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from emerge.exceptions import ModelError
from emerge.models.types import (
    ApiMode,
    ImagePart,
    ModelRequest,
    ModelResponse,
    ReasoningDelta,
    ReasoningPart,
    Speed,
    StreamDone,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCallDelta,
)

logger = logging.getLogger(__name__)

_STREAM_WORD = re.compile(r"\bstream", re.IGNORECASE)


class EmptyStream(ModelError):
    """A streamed request was answered without a single event: the service ignored `stream`."""


class ModelProvider(ABC):
    """One API mode of one provider.

    Every request is streamed first. A provider implements one streamed request, one request without
    streaming, and the test for an error that means "this service will not stream"; this class
    decides which to send and turns a whole response into the same events a stream would give.
    """

    provider: str
    api_mode: ApiMode
    # Request parameters for each speed tier this api_mode supports besides "standard".
    speeds: dict[str, dict[str, Any]] = {}
    # False once this service refused to stream; its requests then go without streaming.
    streaming: bool = True

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Deltas as they arrive, then one StreamDone carrying the complete response.

        When the service refuses to stream, or answers a streamed request with no events at all,
        the request is sent again without streaming and its content comes out as one delta per
        part. Only a refusal before the first event counts: later failures are errors.
        """
        if self.streaming:
            produced = False
            try:
                async for event in self._stream_once(request):
                    produced = True
                    yield event
                return
            except Exception as error:
                if produced or not self._refused_streaming(error):
                    raise
                refusal = error
            response = await self._generate_once(request)
            self._stop_streaming(refusal)
        else:
            response = await self._generate_once(request)
        for event in events_of_response(response):
            yield event

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """The complete response; the request is the same one `stream()` sends."""
        response: ModelResponse | None = None
        async for event in self.stream(request):
            if isinstance(event, StreamDone):
                response = event.response
        if response is None:
            raise ModelError(f"{self.provider} / {self.api_mode} stream ended without a final response")
        return response

    @abstractmethod
    def _stream_once(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one streamed request: deltas, then StreamDone. Raise EmptyStream if no event came."""

    @abstractmethod
    async def _generate_once(self, request: ModelRequest) -> ModelResponse:
        """Send one request without streaming; nothing arrives until the model is done."""

    @abstractmethod
    def _refuses_streaming(self, error: Exception) -> bool:
        """Whether `error`, raised by a streamed request, says the service will not stream it."""

    @abstractmethod
    async def probe_api_compatibility(self) -> None:
        """Raise ModelAPIModeError if the service does not support this provider's API mode."""

    async def aclose(self) -> None:
        pass

    async def _probe_streaming_first(self, ping: Callable[[bool], Awaitable[None]]) -> None:
        """Send a provider's minimal request streamed; if the service refuses to stream, send it plain."""
        if self.streaming:
            try:
                await ping(True)
                return
            except Exception as error:
                if not self._refused_streaming(error):
                    raise
                refusal = error
            await ping(False)
            self._stop_streaming(refusal)
        else:
            await ping(False)

    def _refused_streaming(self, error: Exception) -> bool:
        return isinstance(error, EmptyStream) or self._refuses_streaming(error)

    def _stop_streaming(self, refusal: Exception) -> None:
        self.streaming = False
        logger.warning(
            "%s / %s refused to stream (%s: %s); its requests go without streaming from now on",
            self.provider,
            self.api_mode,
            type(refusal).__name__,
            refusal,
        )


def mentions_stream(text: str | None) -> bool:
    """Whether an error message is about streaming: "stream", "streaming", "stream_options", not "upstream"."""
    return bool(text and _STREAM_WORD.search(text))


def events_of_response(response: ModelResponse) -> Iterator[StreamEvent]:
    """The events a stream of `response` would have given: one delta per content part, then StreamDone."""
    for position, part in enumerate(response.message.content):
        if isinstance(part, ReasoningPart) and part.text:
            yield ReasoningDelta(part=position, text=part.text)
        elif isinstance(part, TextPart) and part.text:
            yield TextDelta(part=position, text=part.text)
    for index, call in enumerate(response.message.tool_calls):
        raw = call.arguments.get("_raw")
        arguments = raw if isinstance(raw, str) and len(call.arguments) == 1 else json.dumps(call.arguments, ensure_ascii=False)
        yield ToolCallDelta(index=index, id=call.id, name=call.name, arguments=arguments)
    yield StreamDone(response=response)


def require_parsed(response: Any, base_url: Any) -> None:
    """The SDKs hand back a body that is not JSON as plain text; report it here, not as a confusing error later."""
    if isinstance(response, str):
        raise ModelError(
            f"{base_url} answered a request without streaming with something other than JSON "
            f"(it starts {response[:80]!r})"
        )


def without_read_limit(timeout: Any) -> Any:
    """The client's timeout minus the read limit, for a request that receives nothing until the model is done.

    A streamed request hears from the service the whole time (content or heartbeats), so a long
    silence means the connection is gone. Without streaming, silence is normal however long the
    model thinks, so only connecting stays limited.
    """
    return type(timeout)(connect=timeout.connect, read=None, write=timeout.write, pool=timeout.pool)


def speed_params(provider: ModelProvider, speed: Speed) -> dict[str, Any]:
    if speed == "standard":
        return {}
    if speed not in provider.speeds:
        raise ModelError(f"{provider.provider} / {provider.api_mode} does not support speed {speed!r}")
    return provider.speeds[speed]


def read_image(image: ImagePart, workspace: Path | None) -> tuple[str, str]:
    """Return (media_type, base64 data) for an image path."""
    path = Path(image.path).expanduser()
    if not path.is_absolute() and workspace is not None:
        path = workspace / path
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ModelError(f"cannot read image {image.path}: {e}") from None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return mime, base64.b64encode(data).decode("ascii")


def image_data_url(image: ImagePart, workspace: Path | None) -> str:
    mime, data = read_image(image, workspace)
    return f"data:{mime};base64,{data}"


def parse_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_raw": raw}
