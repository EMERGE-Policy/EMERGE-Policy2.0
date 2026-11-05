import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from emerge.config import AnthropicOptions, ModelConfig
from emerge.exceptions import ModelAPIModeError, ModelError
from emerge.models.base import (
    EmptyStream,
    ModelProvider,
    mentions_stream,
    read_image,
    require_parsed,
    speed_params,
    without_read_limit,
)
from emerge.models.types import (
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

# Content blocks that become parts of the message content, in order; tool_use becomes a tool call.
_THINKING_BLOCKS = ("thinking", "redacted_thinking")

_FINISH: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
}


class AnthropicMessagesProvider(ModelProvider):
    provider = "anthropic"
    api_mode = "anthropic_messages"
    speeds = {"fast": {"speed": "fast"}}

    def __init__(
        self,
        client: AsyncAnthropic,
        model: ModelConfig,
        provider_options: AnthropicOptions,
        image_root: Path | None = None,
    ) -> None:
        self.client = client
        self.model = model
        # Options only this provider understands, from providers.<name>.options.
        self.provider_options = provider_options
        # Where relative image paths in messages are read from.
        self.image_root = image_root

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        params = await self._build_request_params(request)
        # Block index -> position in the message content (thinking and text blocks, as in _parse), and
        # -> position among tool calls, so ToolCallDelta.index matches the final tool_calls order.
        parts: dict[int, int] = {}
        tool_index: dict[int, int] = {}
        received = False
        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                received = True
                match event.type:
                    case "content_block_start":
                        block = event.content_block
                        if block.type in _THINKING_BLOCKS or block.type == "text":
                            parts.setdefault(event.index, len(parts))
                        elif block.type == "tool_use":
                            index = tool_index.setdefault(event.index, len(tool_index))
                            yield ToolCallDelta(index=index, id=block.id, name=block.name)
                    case "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TextDelta(part=parts.setdefault(event.index, len(parts)), text=delta.text)
                        elif delta.type == "thinking_delta" and delta.thinking:
                            yield ReasoningDelta(part=parts.setdefault(event.index, len(parts)), text=delta.thinking)
                        elif delta.type == "input_json_delta" and event.index in tool_index:
                            yield ToolCallDelta(index=tool_index[event.index], arguments=delta.partial_json)
            # Without a single event the SDK has no message to finish and would fail an assertion.
            if not received:
                raise EmptyStream(f"{self.client.base_url} answered a streamed request without any event")
            final = await stream.get_final_message()
        yield StreamDone(response=self._parse(final))

    async def _generate_once(self, request: ModelRequest) -> ModelResponse:
        # An explicit timeout also keeps the SDK from refusing long non-streaming requests itself.
        message = await self.client.messages.create(
            **await self._build_request_params(request), timeout=without_read_limit(self.client.timeout)
        )
        require_parsed(message, self.client.base_url)
        return self._parse(message)

    def _refuses_streaming(self, error: Exception) -> bool:
        # Anthropic's own API always streams; a relay or gateway in front of it may not, and says so in text.
        return isinstance(error, (anthropic.BadRequestError, anthropic.UnprocessableEntityError)) and mentions_stream(
            error.message
        )

    async def probe_api_compatibility(self) -> None:
        try:
            await self._probe_streaming_first(self._ping)
        except (anthropic.NotFoundError, anthropic.UnprocessableEntityError) as e:
            raise ModelAPIModeError(f"{self.client.base_url} does not support the anthropic messages API: {e}") from None
        except anthropic.APIStatusError as e:
            if e.status_code == 405:
                raise ModelAPIModeError(f"{self.client.base_url} does not support the anthropic messages API: {e}") from None
            raise

    async def aclose(self) -> None:
        await self.client.close()

    async def _ping(self, stream: bool) -> None:
        params: dict[str, Any] = {"model": self.model.name, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]}
        if not stream:
            await self.client.messages.create(**params, timeout=without_read_limit(self.client.timeout))
            return
        async with self.client.messages.stream(**params) as events:
            received = [event.type async for event in events]
            if not received:
                raise EmptyStream(f"{self.client.base_url} answered a streamed request without any event")

    async def _build_request_params(self, request: ModelRequest) -> dict[str, Any]:
        options = self.model.options
        system, messages = await asyncio.to_thread(self._messages, request)
        params: dict[str, Any] = {
            "model": self.model.name,
            "max_tokens": request.max_output_tokens or options.max_output_tokens,
            "messages": messages,
            # Anthropic only caches when asked; automatic caching moves the breakpoint to the end as history grows.
            # It looks a prefix up by its content, so request.cache_key is not needed and not sent.
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            params["system"] = system
        extra_body: dict[str, Any] = {}
        thinking = self.provider_options.thinking
        if thinking.type == "adaptive":
            # Newer Claude models return empty thinking blocks unless asked for the summary.
            params["thinking"] = {"type": "adaptive", "display": thinking.display}
            effort = request.reasoning_effort or options.reasoning_effort
            if effort:
                params["output_config"] = {"effort": effort}
        else:
            # The 1.x SDK dropped temperature from the signature; it is still accepted by models without thinking.
            extra_body["temperature"] = options.temperature if request.temperature is None else request.temperature
        if request.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in request.tools
            ]
        if request.tool_choice:
            params["tool_choice"] = {"type": "any" if request.tool_choice == "required" else request.tool_choice}
        params |= speed_params(self, request.speed or options.speed)
        extra = dict(request.extra)
        if extra_body or "extra_body" in extra:
            extra["extra_body"] = extra_body | extra.get("extra_body", {})
        return params | extra

    def _messages(self, request: ModelRequest) -> tuple[str, list[dict[str, Any]]]:
        system: list[str] = []
        out: list[dict[str, Any]] = []

        def append(role: str, blocks: list[dict[str, Any]]) -> None:
            # Anthropic requires alternating roles; tool results and the next user turn share one user message.
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        # Images inside tool_result content are dropped by some relays, so they follow the tool results as
        # sibling blocks in the same user message (tool_result blocks must come first there).
        tool_images: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role != "tool" and tool_images:
                append("user", tool_images)
                tool_images = []
            match message.role:
                case "system":
                    system.append(message.text)
                case "assistant":
                    native = [n.data for n in message.native_items if self._matches_provider_and_api_mode(n)]
                    if native:
                        append("assistant", list(native))
                        continue
                    blocks: list[dict[str, Any]] = [{"type": "text", "text": message.text}] if message.text else []
                    blocks += [
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in message.tool_calls
                    ]
                    append("assistant", blocks)
                case "tool":
                    append("user", [{"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.text}])
                    tool_images += [self._block(p) for p in message.images]
                case "user":
                    append("user", [self._block(p) for p in message.content])
        if tool_images:
            append("user", tool_images)
        if request.native_items:
            append("assistant", [n.data for n in request.native_items])
        return "\n\n".join(system), out

    def _block(self, part: TextPart | ImagePart) -> dict[str, Any]:
        if isinstance(part, TextPart):
            return {"type": "text", "text": part.text}
        media_type, data = read_image(part, self.image_root)
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}

    def _matches_provider_and_api_mode(self, item: NativeItem) -> bool:
        return item.provider == self.provider and item.api_mode == self.api_mode

    def _parse(self, message: Any) -> ModelResponse:
        if message.stop_reason is None and not message.content:
            raise ModelError("anthropic response has no content")
        # Every content block (thinking with its signature, redacted_thinking, text, tool_use) is kept verbatim.
        native = [
            NativeItem(provider=self.provider, api_mode="anthropic_messages", data=block.model_dump(mode="json", exclude_unset=True))
            for block in message.content
        ]
        parts: list[Part] = []
        for block in message.content:
            if block.type == "thinking":
                # The readable summary; the full thinking travels encrypted in the signature.
                parts.append(ReasoningPart(text=block.thinking))
            elif block.type == "redacted_thinking":
                parts.append(ReasoningPart())
            elif block.type == "text":
                parts.append(TextPart(text=block.text))
        calls = [
            ToolCall(id=block.id, name=block.name, arguments=block.input if isinstance(block.input, dict) else {"_raw": str(block.input)})
            for block in message.content
            if block.type == "tool_use"
        ]
        return ModelResponse(
            message=Message(role="assistant", content=parts, tool_calls=calls, native_items=native),
            native_items=native,
            response_id=message.id,
            usage=_usage(message.usage),
            finish_reason=_FINISH.get(message.stop_reason or "end_turn", "other"),
        )


def _usage(usage: Any) -> Usage:
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    # Anthropic's input_tokens excludes cache reads and writes; report the full input like the OpenAI providers do.
    input_tokens = (usage.input_tokens or 0) + cache_read + cache_write
    output_tokens = usage.output_tokens or 0
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cached_tokens=cache_read,
        speed=getattr(usage, "speed", None),
    )
