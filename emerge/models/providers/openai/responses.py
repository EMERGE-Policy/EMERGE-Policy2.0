import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import openai
from openai import AsyncOpenAI

from emerge.config import ModelConfig, OpenAIOptions
from emerge.exceptions import ModelAPIModeError, ModelError
from emerge.models.base import (
    EmptyStream,
    ModelProvider,
    image_data_url,
    mentions_stream,
    parse_arguments,
    require_parsed,
    speed_params,
    without_read_limit,
)
from emerge.models.types import (
    REASONING_SEPARATOR,
    FinishReason,
    ImagePart,
    Message,
    ModelRequest,
    ModelResponse,
    NativeItem,
    Part,
    ReasoningDelta,
    ReasoningPart,
    StreamDone,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCall,
    ToolCallDelta,
    Usage,
)

ENCRYPTED_REASONING = "reasoning.encrypted_content"
# Output items that become parts of the message content; function calls become tool calls.
_CONTENT_ITEMS = ("reasoning", "message")


class OpenAIResponsesProvider(ModelProvider):
    provider = "openai"
    api_mode = "responses"
    speeds = {"fast": {"service_tier": "fast"}, "ultrafast": {"service_tier": "ultrafast"}}

    def __init__(
        self, client: AsyncOpenAI, model: ModelConfig, provider_options: OpenAIOptions, image_root: Path | None = None
    ) -> None:
        self.client = client
        self.model = model
        # Options only this provider understands, from providers.<name>.options.
        self.provider_options = provider_options
        # Where relative image paths in messages are read from.
        self.image_root = image_root

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        stream = await self.client.responses.create(**await self._build_request_params(request), stream=True)
        # Keyed by output_index because some servers omit item ids in stream events. Every reasoning
        # and message item is one part of the message content, in output order, as in _parse.
        parts: dict[int, int] = {}
        tool_index: dict[int, int] = {}
        final = None
        received = False
        async with stream:
            async for event in stream:
                received = True
                match event.type:
                    case "response.output_item.added":
                        if event.item.type in _CONTENT_ITEMS:
                            parts.setdefault(event.output_index, len(parts))
                        elif event.item.type == "function_call":
                            index = tool_index.setdefault(event.output_index, len(tool_index))
                            yield ToolCallDelta(index=index, id=event.item.call_id, name=event.item.name)
                    case "response.reasoning_summary_part.added" if event.summary_index > 0:
                        part = parts.setdefault(event.output_index, len(parts))
                        yield ReasoningDelta(part=part, text=REASONING_SEPARATOR)
                    case "response.reasoning_summary_text.delta":
                        yield ReasoningDelta(part=parts.setdefault(event.output_index, len(parts)), text=event.delta)
                    case "response.output_text.delta" | "response.refusal.delta":
                        yield TextDelta(part=parts.setdefault(event.output_index, len(parts)), text=event.delta)
                    case "response.function_call_arguments.delta":
                        index = tool_index.setdefault(event.output_index, len(tool_index))
                        yield ToolCallDelta(index=index, arguments=event.delta)
                    case "response.completed" | "response.incomplete":
                        final = event.response
                    case "response.failed":
                        error = event.response.error
                        raise ModelError(f"response failed: {error.message if error else 'unknown error'}")
                    case "error":
                        raise ModelError(f"response stream error: {event.message}")
        if not received:
            raise EmptyStream(f"{self.client.base_url} answered a streamed request without any event")
        if final is None:
            raise ModelError("response stream ended without a final response")
        yield StreamDone(response=self._parse(final))

    async def _generate_once(self, request: ModelRequest) -> ModelResponse:
        response = await self.client.responses.create(
            **await self._build_request_params(request), timeout=without_read_limit(self.client.timeout)
        )
        require_parsed(response, self.client.base_url)
        return self._parse(response)

    def _refuses_streaming(self, error: Exception) -> bool:
        # OpenAI names the parameter it refuses, e.g. 400 "Your organization must be verified to
        # stream this model" with param "stream"; other services only say so in the message.
        return isinstance(error, (openai.BadRequestError, openai.UnprocessableEntityError)) and (
            error.param == "stream" or mentions_stream(error.message)
        )

    async def probe_api_compatibility(self) -> None:
        try:
            await self._probe_streaming_first(self._ping)
        except (openai.NotFoundError, openai.UnprocessableEntityError) as e:
            raise ModelAPIModeError(f"{self.client.base_url} does not support the responses API: {e}") from None
        except openai.APIStatusError as e:
            if e.status_code == 405:
                raise ModelAPIModeError(f"{self.client.base_url} does not support the responses API: {e}") from None
            raise

    async def aclose(self) -> None:
        await self.client.close()

    async def _ping(self, stream: bool) -> None:
        params: dict[str, Any] = {"model": self.model.name, "input": "ping", "max_output_tokens": 16, "store": False}
        if not stream:
            await self.client.responses.create(**params, timeout=without_read_limit(self.client.timeout))
            return
        events = await self.client.responses.create(**params, stream=True)
        async with events:
            received = [event.type async for event in events]
        if not received:
            raise EmptyStream(f"{self.client.base_url} answered a streamed request without any event")

    async def _build_request_params(self, request: ModelRequest) -> dict[str, Any]:
        options = self.model.options
        params: dict[str, Any] = {
            "model": self.model.name,
            "input": await asyncio.to_thread(self._input, request),
            # Nothing is kept on the server: the reasoning comes back encrypted in its output item, and
            # the next request carries on from it by sending that item back verbatim in the input.
            "store": False,
            "include": [ENCRYPTED_REASONING],
            "max_output_tokens": request.max_output_tokens or options.max_output_tokens,
            "temperature": options.temperature if request.temperature is None else request.temperature,
        }
        effort = request.reasoning_effort or options.reasoning_effort
        if effort:
            params["reasoning"] = {"effort": effort, "summary": self.provider_options.reasoning_summary or None}
        if request.tools:
            params["tools"] = [
                {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters, "strict": False}
                for t in request.tools
            ]
        if request.tool_choice:
            params["tool_choice"] = request.tool_choice
        if request.cache_key:
            # Requests with the same key are routed to the same cache.
            params["prompt_cache_key"] = request.cache_key
        params |= speed_params(self, request.speed or options.speed)
        return params | request.extra

    def _input(self, request: ModelRequest) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        # function_call_output only carries text, so tool-result images follow the tool outputs as a user message.
        tool_images: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role != "tool" and tool_images:
                items.append({"role": "user", "content": tool_images})
                tool_images = []
            match message.role:
                case "assistant":
                    native = [n.data for n in message.native_items if self._matches_provider_and_api_mode(n)]
                    if native:
                        items.extend(native)
                        continue
                    if message.text:
                        items.append({"role": "assistant", "content": message.text})
                    for call in message.tool_calls:
                        items.append({
                            "type": "function_call",
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        })
                case "tool":
                    items.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": message.text})
                    tool_images += [self._image(p) for p in message.images]
                case _:
                    items.append({"role": message.role, "content": [self._part(p) for p in message.content]})
        if tool_images:
            items.append({"role": "user", "content": tool_images})
        items += [n.data for n in request.native_items]
        return items

    def _part(self, part: TextPart | ImagePart) -> dict[str, Any]:
        if isinstance(part, TextPart):
            return {"type": "input_text", "text": part.text}
        return self._image(part)

    def _image(self, image: ImagePart) -> dict[str, Any]:
        return {"type": "input_image", "image_url": image_data_url(image, self.image_root), "detail": image.detail or "auto"}

    def _matches_provider_and_api_mode(self, item: NativeItem) -> bool:
        return item.provider == self.provider and item.api_mode == self.api_mode

    def _parse(self, response: Any) -> ModelResponse:
        native: list[NativeItem] = []
        parts: list[Part] = []
        calls: list[ToolCall] = []
        for item in response.output:
            # exclude_unset keeps exactly the fields the server sent, encrypted_content included.
            native.append(NativeItem(provider=self.provider, api_mode="responses", data=item.model_dump(mode="json", exclude_unset=True)))
            if item.type == "reasoning":
                # The readable summary only; the reasoning itself travels encrypted in the native item.
                summary = [entry.text for entry in item.summary or []]
                parts.append(ReasoningPart(text=REASONING_SEPARATOR.join(summary)))
            elif item.type == "message":
                texts = [
                    content.text if content.type == "output_text" else content.refusal
                    for content in item.content
                    if content.type in ("output_text", "refusal")
                ]
                parts.append(TextPart(text="".join(texts)))
            elif item.type == "function_call":
                calls.append(ToolCall(id=item.call_id, name=item.name, arguments=parse_arguments(item.arguments)))
        message = Message(role="assistant", content=parts, tool_calls=calls, native_items=native)
        return ModelResponse(
            message=message,
            native_items=native,
            response_id=response.id,
            usage=_usage(response.usage, response.service_tier),
            finish_reason=_finish_reason(response, bool(calls)),
        )


def _finish_reason(response: Any, has_calls: bool) -> FinishReason:
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        return "length" if reason == "max_output_tokens" else "content_filter" if reason == "content_filter" else "other"
    return "tool_calls" if has_calls else "stop"


def _usage(usage: Any, service_tier: str | None) -> Usage:
    if usage is None:
        return Usage(speed=service_tier)
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return Usage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        total_tokens=usage.total_tokens or 0,
        cached_tokens=getattr(input_details, "cached_tokens", None),
        reasoning_tokens=getattr(output_details, "reasoning_tokens", None),
        speed=service_tier,
    )
