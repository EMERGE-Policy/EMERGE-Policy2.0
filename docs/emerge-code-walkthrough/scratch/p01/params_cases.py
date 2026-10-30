import asyncio
import json
import tempfile
from pathlib import Path

from harness import *  # noqa
from emerge.models import Message, ModelRequest, NativeItem, ToolCall, ToolSpec, ReasoningPart, TextPart

tmp = Path(tempfile.mkdtemp())
(tmp / "a.jpg").write_bytes(b"\xff\xd8\xff")
(tmp / "noext").write_bytes(b"\x89PNG")


def short(o):
    s = json.dumps(o, ensure_ascii=False)
    import re
    return re.sub(r"base64,[A-Za-z0-9+/=]+|\"data\": \"[A-Za-z0-9+/=]+\"", "<b64>", s)


asst_native_resp = Message(role="assistant", content=[ReasoningPart(text="t"), TextPart(text="prev")], tool_calls=[ToolCall(id="c1", name="get", arguments={"a": 1})],
                           native_items=[NativeItem(provider="openai", api_mode="responses", data={"type": "reasoning", "id": "rs", "encrypted_content": "E"}),
                                         NativeItem(provider="openai", api_mode="responses", data={"type": "function_call", "call_id": "c1", "name": "get", "arguments": "{\"a\":1}"})])
MSGS = [
    Message.system("SYS1"),
    Message.user("look", images=[str(tmp / "a.jpg")], detail="low"),
    asst_native_resp,
    Message.tool("c1", "result text", images=[str(tmp / "noext")]),
    Message.tool("c2", "second result"),
    Message.user("next"),
    Message.system("SYS2"),
    Message(role="assistant", content=[ReasoningPart(text="only thinking")]),
]
REQ = ModelRequest(messages=MSGS, tools=[ToolSpec(name="get", description="d", parameters={"type": "object", "properties": {}})],
                   tool_choice="required", speed="fast", native_items=[NativeItem(provider="x", api_mode="chat_completions", data={"type": "foreign"})],
                   extra={"prompt_cache_key": "sess", "temperature": 0.7})


async def main():
    s = Script()
    for name, p in [("responses", responses_provider(s)), ("chat", chat_provider(s)), ("anthropic", anthropic_provider(s)),
                    ("anthropic disabled", anthropic_provider(s, thinking="disabled"))]:
        params = await p._build_request_params(REQ)
        print(f"=== {name}")
        for k, v in params.items():
            if k in ("input", "messages"):
                for item in v:
                    print("   ", k, short(item))
            else:
                print("   ", k, "=", short(v))
    # empty effort / summary
    p = responses_provider(s, summary="", reasoning_effort="")
    print("=== responses effort='' ->", "reasoning" in await p._build_request_params(ModelRequest(messages=[Message.user("x")])))
    p = responses_provider(s, summary="")
    print("=== responses summary='' ->", (await p._build_request_params(ModelRequest(messages=[Message.user("x")])))["reasoning"])
    print("=== responses request effort='' with config medium ->", (await p._build_request_params(ModelRequest(messages=[Message.user("x")], reasoning_effort="")))["reasoning"])
    p = anthropic_provider(s, display="omitted", reasoning_effort="")
    pr = await p._build_request_params(ModelRequest(messages=[Message.user("x")], temperature=0.3, extra={"extra_body": {"temperature": 0.9, "foo": 1}}))
    print("=== anthropic adaptive, effort='', request temperature=0.3, extra_body ->", {k: v for k, v in pr.items() if k != "messages"})
    p = anthropic_provider(s, thinking="disabled")
    pr = await p._build_request_params(ModelRequest(messages=[Message.user("x")], temperature=0.3, extra={"extra_body": {"temperature": 0.9, "foo": 1}}))
    print("=== anthropic disabled, request temperature=0.3, extra_body ->", {k: v for k, v in pr.items() if k != "messages"})
    # assistant without text/tool calls on each
    only_think = [Message.user("q"), Message(role="assistant", content=[ReasoningPart(text="x")]), Message.user("again")]
    for name, p in [("responses", responses_provider(s)), ("chat", chat_provider(s)), ("anthropic", anthropic_provider(s))]:
        pr = await p._build_request_params(ModelRequest(messages=only_think))
        print(f"=== {name} assistant with only reasoning ->", short(pr.get("input") or pr.get("messages")))
    # user message with no text no image
    for name, p in [("responses", responses_provider(s)), ("chat", chat_provider(s)), ("anthropic", anthropic_provider(s))]:
        pr = await p._build_request_params(ModelRequest(messages=[Message.user("")]))
        print(f"=== {name} Message.user('') ->", short(pr.get("input") or pr.get("messages")))
    # tool_choice none / auto anthropic
    p = anthropic_provider(s)
    for tc in ("auto", "none", "required"):
        pr = await p._build_request_params(ModelRequest(messages=[Message.user("x")], tool_choice=tc))
        print("=== anthropic tool_choice", tc, "->", pr["tool_choice"])
    # system image dropped?
    sysimg = Message(role="system", content=[TextPart(text="S"), *Message.user("", images=[str(tmp / "a.jpg")]).content])
    for name, p in [("responses", responses_provider(s)), ("chat", chat_provider(s)), ("anthropic", anthropic_provider(s))]:
        pr = await p._build_request_params(ModelRequest(messages=[sysimg, Message.user("u")]))
        print(f"=== {name} system with image ->", short(pr.get("input") or pr.get("messages")), "system=", short(pr.get("system")))

asyncio.run(main())
