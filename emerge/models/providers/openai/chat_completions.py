import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import openai
from openai import AsyncOpenAI

from emerge.config import ModelConfig
from emerge.exceptions import ModelAPIModeError
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

_FINISH: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


class OpenAIChatCompletionsProvider(ModelProvider):
    provider = "openai"
    api_mode = "chat_completions"
    # Ultrafast is served by the Responses API only.
    speeds = {"fast": {"service_tier": "priority"}}

    def __init__(self, client: AsyncOpenAI, model: ModelConfig, workspace: Path | None = None) -> None:
        self.client = client
        self.model = model
        self.workspace = workspace

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        params = await self._build_request_params(request)
        stream = await self.client.chat.completions.create(**params, stream=True, stream_options={"include_usage": True})
        text: list[str] = []
        refusal: list[str] = []
        reasoning: list[str] = []
        # The message content in arrival order: a new part whenever the stream switches between
        # thinking and answer, each (kind, text so far).
        parts: list[tuple[str, list[str]]] = []
        calls: dict[int, dict[str, Any]] = {}
        response_id: str | None = None
        finish: str | None = None
        service_tier: str | None = None
        raw_usage: Any = None
        received = False

        def extend(kind: str, piece: str) -> int:
            if not parts or parts[-1][0] != kind:
                parts.append((kind, []))
            parts[-1][1].append(piece)
            return len(parts) - 1

        async with stream:
            async for chunk in stream:
                received = True
                response_id = response_id or chunk.id
                service_tier = chunk.service_tier or service_tier
                if chunk.usage is not None:
                    raw_usage = chunk.usage
                for choice in chunk.choices:
                    delta = choice.delta
                    finish = choice.finish_reason or finish
                    # Non-standard field some compatible servers stream reasoning in.
                    extra_reasoning = (delta.model_extra or {}).get("reasoning_content")
                    if extra_reasoning:
                        reasoning.append(extra_reasoning)
                        yield ReasoningDelta(part=extend("reasoning", extra_reasoning), text=extra_reasoning)
                    # A refusal is the answer the user sees; the native message keeps it apart, as sent.
                    for pieces, piece in ((text, delta.content), (refusal, delta.refusal)):
                        if piece:
                            pieces.append(piece)
                            yield TextDelta(part=extend("text", piece), text=piece)
                    for tc in delta.tool_calls or []:
                        call = calls.setdefault(tc.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        fn = tc.function
                        if tc.id:
                            call["id"] = tc.id
                        if fn and fn.name:
                            call["function"]["name"] += fn.name
                        if fn and fn.arguments:
                            call["function"]["arguments"] += fn.arguments
                        yield ToolCallDelta(index=tc.index, id=tc.id, name=fn.name if fn else None, arguments=(fn.arguments or "") if fn else "")
        if not received:
            raise EmptyStream(f"{self.client.base_url} answered a streamed request without any event")
        raw: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
        if refusal:
            raw["refusal"] = "".join(refusal)
        if reasoning:
            raw["reasoning_content"] = "".join(reasoning)
        if calls:
            raw["tool_calls"] = [calls[i] for i in sorted(calls)]
        content: list[Part] = [
            ReasoningPart(text="".join(pieces)) if kind == "reasoning" else TextPart(text="".join(pieces))
            for kind, pieces in parts
        ]
        yield StreamDone(response=self._response(raw, response_id, _usage(raw_usage, service_tier), finish, content))

    async def _generate_once(self, request: ModelRequest) -> ModelResponse:
        completion = await self.client.chat.completions.create(
            **await self._build_request_params(request), timeout=without_read_limit(self.client.timeout)
        )
        require_parsed(completion, self.client.base_url)
        choice = completion.choices[0]
        raw = choice.message.model_dump(mode="json", exclude_unset=True)
        return self._response(raw, completion.id, _usage(completion.usage, completion.service_tier), choice.finish_reason)

    def _refuses_streaming(self, error: Exception) -> bool:
        # Also covers compatible servers that reject stream_options, which only streaming sends.
        return isinstance(error, (openai.BadRequestError, openai.UnprocessableEntityError)) and (
            error.param in ("stream", "stream_options") or mentions_stream(error.message)
        )

    async def probe_api_compatibility(self) -> None:
        try:
            await self._probe_streaming_first(self._ping)
        except (openai.NotFoundError, openai.UnprocessableEntityError) as e:
            raise ModelAPIModeError(f"{self.client.base_url} does not support the chat completions API: {e}") from None
        except openai.APIStatusError as e:
            if e.status_code == 405:
                raise ModelAPIModeError(f"{self.client.base_url} does not support the chat completions API: {e}") from None
            raise

    async def aclose(self) -> None:
        await self.client.close()

    async def _ping(self, stream: bool) -> None:
        params: dict[str, Any] = {
            "model": self.model.name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_completion_tokens": 16,
        }
        if not stream:
            await self.client.chat.completions.create(**params, timeout=without_read_limit(self.client.timeout))
            return
        chunks = await self.client.chat.completions.create(**params, stream=True)
        async with chunks:
            received = [chunk.id async for chunk in chunks]
        if not received:
            raise EmptyStream(f"{self.client.base_url} answered a streamed request without any event")

    async def _build_request_params(self, request: ModelRequest) -> dict[str, Any]:
        options = self.model.options
        params: dict[str, Any] = {
            "model": self.model.name,
            "messages": await asyncio.to_thread(self._messages, request),
            "max_completion_tokens": request.max_output_tokens or options.max_output_tokens,
            "temperature": options.temperature if request.temperature is None else request.temperature,
        }
        effort = request.reasoning_effort or options.reasoning_effort
        if effort:
            params["reasoning_effort"] = effort
        if request.tools:
            params["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in request.tools
            ]
        if request.tool_choice:
            params["tool_choice"] = request.tool_choice
        params |= speed_params(self, request.speed or options.speed)
        return params | request.extra

    def _messages(self, request: ModelRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # Tool messages carry text only and must directly follow the assistant turn, so their images go after them.
        tool_images: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role != "tool" and tool_images:
                out.append({"role": "user", "content": tool_images})
                tool_images = []
            match message.role:
                case "assistant":
                    native = [n.data for n in message.native_items if self._matches_provider_and_api_mode(n)]
                    if native:
                        out.extend(native)
                        continue
                    item: dict[str, Any] = {"role": "assistant", "content": message.text or None}
                    if message.tool_calls:
                        item["tool_calls"] = [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                            }
                            for c in message.tool_calls
                        ]
                    out.append(item)
                case "tool":
                    out.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": message.text})
                    tool_images += [self._image(p) for p in message.images]
                case "system":
                    out.append({"role": "system", "content": message.text})
                case "user":
                    if message.images:
                        out.append({"role": "user", "content": [self._part(p) for p in message.content]})
                    else:
                        out.append({"role": "user", "content": message.text})
        if tool_images:
            out.append({"role": "user", "content": tool_images})
        out += [n.data for n in request.native_items]
        return out

    def _part(self, part: TextPart | ImagePart) -> dict[str, Any]:
        if isinstance(part, TextPart):
            return {"type": "text", "text": part.text}
        return self._image(part)

    def _image(self, image: ImagePart) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": image_data_url(image, self.workspace), "detail": image.detail or "auto"}}

    def _matches_provider_and_api_mode(self, item: NativeItem) -> bool:
        return item.provider == self.provider and item.api_mode == self.api_mode

    def _response(
        self,
        raw: dict[str, Any],
        response_id: str | None,
        usage: Usage,
        finish: str | None,
        content: list[Part] | None = None,
    ) -> ModelResponse:
        """`content` is the order a stream delivered; a whole response puts the thinking first."""
        calls = [
            ToolCall(id=c["id"], name=c["function"]["name"], arguments=parse_arguments(c["function"].get("arguments")))
            for c in raw.get("tool_calls") or []
        ]
        if content is None:
            reasoning = raw.get("reasoning_content")
            answer = "".join(piece for piece in (raw.get("content"), raw.get("refusal")) if piece)
            content = [ReasoningPart(text=reasoning)] if reasoning else []
            content += [TextPart(text=answer)] if answer else []
        native = [NativeItem(provider=self.provider, api_mode="chat_completions", data=raw)]
        message = Message(role="assistant", content=content, tool_calls=calls, native_items=native)
        return ModelResponse(
            message=message,
            native_items=native,
            response_id=response_id,
            usage=usage,
            finish_reason=_FINISH.get(finish or "stop", "other"),
        )


def _usage(usage: Any, service_tier: str | None) -> Usage:
    if usage is None:
        return Usage(speed=service_tier)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return Usage(
        input_tokens=usage.prompt_tokens or 0,
        output_tokens=usage.completion_tokens or 0,
        total_tokens=usage.total_tokens or 0,
        cached_tokens=getattr(prompt_details, "cached_tokens", None),
        reasoning_tokens=getattr(completion_details, "reasoning_tokens", None),
        speed=service_tier,
    )
