from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["system", "user", "assistant", "tool"]
ApiMode = Literal["chat_completions", "responses", "anthropic_messages"]
ImageDetail = Literal["auto", "low", "high"]
Speed = Literal["standard", "fast", "ultrafast"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextPart(_Model):
    type: Literal["text"] = "text"
    text: str


class ImagePart(_Model):
    type: Literal["image"] = "image"
    # Workspace-relative or absolute path; the file is read and encoded only when a request is sent.
    path: str
    detail: ImageDetail | None = None


class ReasoningPart(_Model):
    """What the model thought before or between its answers, as readable text.

    Providers return a summary, not the raw chain of thought; the text is empty when the model
    thought but returned none (a short OpenAI reasoning step, Claude with `display: "omitted"`,
    redacted thinking). Only assistant messages carry it, and it is never sent to an API: what goes
    back to the provider that produced it, signatures and encrypted reasoning included, is in the
    message's `native_items`.
    """

    type: Literal["reasoning"] = "reasoning"
    text: str = ""


Part = Annotated[TextPart | ImagePart | ReasoningPart, Field(discriminator="type")]

# Between two reasoning parts in `Message.reasoning`, and between the summary paragraphs of one.
REASONING_SEPARATOR = "\n\n"


class ToolCall(_Model):
    id: str
    name: str
    # Arguments the model produced; unparseable JSON is kept as {"_raw": "<text>"} for the tool layer to reject.
    arguments: dict[str, Any] = {}


class ToolSpec(_Model):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class NativeItem(_Model):
    """A provider-native item kept verbatim (reasoning, encrypted_content, ids); never parsed or rewritten."""

    provider: str
    api_mode: ApiMode
    data: dict[str, Any]


class Message(_Model):
    role: Role
    content: list[Part] = []
    tool_calls: list[ToolCall] = []
    tool_call_id: str | None = None
    # Native items that produced this assistant message; sent back in its place when provider and api_mode match.
    native_items: list[NativeItem] = []

    @model_validator(mode="after")
    def _reasoning_only_from_the_model(self) -> "Message":
        if self.role != "assistant" and any(isinstance(p, ReasoningPart) for p in self.content):
            raise ValueError(f"a {self.role} message cannot hold reasoning")
        return self

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.content if isinstance(p, TextPart))

    @property
    def reasoning(self) -> str:
        """The model's thinking in this message, in order; empty when it returned none."""
        return REASONING_SEPARATOR.join(p.text for p in self.content if isinstance(p, ReasoningPart) and p.text)

    @property
    def images(self) -> list[ImagePart]:
        return [p for p in self.content if isinstance(p, ImagePart)]

    @classmethod
    def system(cls, text: str) -> "Message":
        return cls(role="system", content=[TextPart(text=text)])

    @classmethod
    def user(cls, text: str = "", images: list[str] | None = None, detail: ImageDetail | None = None) -> "Message":
        parts: list[Part] = [TextPart(text=text)] if text else []
        parts += [ImagePart(path=p, detail=detail) for p in images or []]
        return cls(role="user", content=parts)

    @classmethod
    def tool(cls, tool_call_id: str, text: str, images: list[str] | None = None) -> "Message":
        parts: list[Part] = [TextPart(text=text)]
        parts += [ImagePart(path=p) for p in images or []]
        return cls(role="tool", content=parts, tool_call_id=tool_call_id)


class Usage(_Model):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    # Tier that served the response, as the provider reported it (Anthropic usage.speed, OpenAI service_tier).
    speed: str | None = None

    def __add__(self, other: "Usage") -> "Usage":
        def opt(a: int | None, b: int | None) -> int | None:
            return None if a is None and b is None else (a or 0) + (b or 0)

        # speed describes a single response and is not carried into totals.
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=opt(self.cached_tokens, other.cached_tokens),
            reasoning_tokens=opt(self.reasoning_tokens, other.reasoning_tokens),
        )


class ModelRequest(_Model):
    messages: list[Message]
    tools: list[ToolSpec] = []
    # Extra native items appended verbatim after the converted messages.
    native_items: list[NativeItem] = []
    tool_choice: Literal["auto", "none", "required"] | None = None
    # Per-request overrides of model.options; None keeps the configured value.
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    speed: Speed | None = None
    # Passed through to the SDK call unchanged.
    extra: dict[str, Any] = {}


FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "other"]


class ModelResponse(_Model):
    message: Message
    native_items: list[NativeItem] = []
    response_id: str | None = None
    usage: Usage = Usage()
    finish_reason: FinishReason = "stop"


# A stream hands out the assistant message's content as it grows. `part` is the position in
# `Message.content` a delta extends: all deltas of one part, joined, are that part's final text.


class TextDelta(_Model):
    type: Literal["text_delta"] = "text_delta"
    part: int = Field(ge=0)
    text: str


class ReasoningDelta(_Model):
    type: Literal["reasoning_delta"] = "reasoning_delta"
    part: int = Field(ge=0)
    text: str


class ToolCallDelta(_Model):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


class StreamDone(_Model):
    type: Literal["done"] = "done"
    response: ModelResponse


StreamEvent = Annotated[TextDelta | ReasoningDelta | ToolCallDelta | StreamDone, Field(discriminator="type")]
