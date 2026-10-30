import json
from emerge.events import Event, EventType
from emerge.models import Message, Usage, ToolCall, ModelResponse, ReasoningPart, TextPart, NativeItem, ToolSpec, ImagePart
from emerge.models.base import mentions_stream, parse_arguments, events_of_response, speed_params
from emerge.models.usage import count_text_tokens, estimate_tokens, estimate_message_tokens, _native_tokens, UsageTracker, _encoding
from pydantic import ValidationError

print("tiktoken encoding:", _encoding())
for t in ["stream", "Streaming not supported", "stream_options", "upstream error", "non-stream", "include_stream", "STREAMS", "", None]:
    print(f"mentions_stream({t!r}) = {mentions_stream(t)}")
for raw in [None, "", "{\"a\":1}", "[1,2]", "null", "{bad", "\"s\""]:
    print(f"parse_arguments({raw!r}) = {parse_arguments(raw)}")
try:
    Message(role="user", content=[ReasoningPart(text="x")])
except ValidationError as e:
    print("user with reasoning:", e.errors()[0]["msg"])
print("user(''):", Message.user("").content, "tool:", Message.tool("c", "").content)
print("text/reasoning:", repr(Message(role="assistant", content=[ReasoningPart(text="a"), TextPart(text="x"), ReasoningPart(), ReasoningPart(text="b"), TextPart(text="y")]).reasoning))
try:
    Message.model_validate({"role": "user", "content": [], "extra": 1})
except ValidationError as e:
    print("extra field on Message:", e.errors()[0]["type"])
u = Usage(input_tokens=1, cached_tokens=None, speed="fast") + Usage(input_tokens=2, cached_tokens=5, reasoning_tokens=None, speed="fast")
print("usage add:", u.model_dump())
print("usage none+none:", (Usage() + Usage()).model_dump())
# events_of_response
resp = ModelResponse(message=Message(role="assistant", content=[ReasoningPart(), TextPart(text=""), TextPart(text="hi")],
                                     tool_calls=[ToolCall(id="1", name="a", arguments={"_raw": "{bad"}), ToolCall(id="2", name="b", arguments={"_raw": "x", "k": 1}), ToolCall(id="3", name="c", arguments={"ü": 1})]))
for e in events_of_response(resp):
    print("  ", e.model_dump(exclude={"response"}))
# token counts
for t in ["a", "abcd", "hello world", "中文字符", "abc中文"]:
    print(f"count_text_tokens({t!r}) = {count_text_tokens(t)}")
m = Message.user("hello world", images=["x.png"], detail="low")
print("user msg est:", estimate_message_tokens(m), "detail None:", estimate_message_tokens(Message.user("", images=["x.png"])))
n = NativeItem(provider="openai", api_mode="responses", data={"type": "reasoning", "encrypted_content": "A" * 400, "summary": []})
print("native tokens:", _native_tokens(n))
am = Message(role="assistant", content=[TextPart(text="x" * 4000)], native_items=[n])
print("assistant with native est:", estimate_message_tokens(am))
print("tools est:", estimate_tokens([], [ToolSpec(name="t", description="d")]))
# Event roundtrip
e = Event(type=EventType.NODE_STATE, run_id="r", payload={"a": [1, {"b": 2}]})
d = json.loads(json.dumps(e.to_dict()))
e2 = Event.from_dict(d)
print("event roundtrip equal:", e2 == e, type(e2.type).__name__)
try:
    Event.from_dict({**d, "extra": 1})
except TypeError as ex:
    print("from_dict extra key:", ex)
try:
    Event.from_dict({"run_id": "r"})
except TypeError as ex:
    print("from_dict no type:", ex)
# asdict deep copies payload
p = {"x": [1]}
e = Event(type="t", payload=p)
dd = e.to_dict(); dd["payload"]["x"].append(2)
print("to_dict copies payload:", p)
