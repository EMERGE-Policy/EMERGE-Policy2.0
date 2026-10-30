import asyncio
import json
import logging

import httpx
import httpx2

from harness import *  # noqa
from emerge.models import Message, ModelRequest, ToolSpec, StreamDone, TextDelta, ReasoningDelta, ToolCallDelta
from emerge.models.base import EmptyStream

logging.basicConfig(level=logging.WARNING, format="LOG %(levelname)s %(name)s: %(message)s")


def show(events):
    out = []
    for e in events:
        if isinstance(e, StreamDone):
            r = e.response
            out.append(("done", [(type(p).__name__, getattr(p, "text", None)) for p in r.message.content], [(c.id, c.name, c.arguments) for c in r.message.tool_calls], r.finish_reason, r.usage.model_dump(), len(r.native_items)))
        else:
            out.append(e.model_dump(exclude_none=True))
    return out


async def collect(p, req):
    return [e async for e in p.stream(req)]


REQ = ModelRequest(messages=[Message.user("hi")])


async def main():
    print("=== Responses: normal stream")
    s = Script(oa_sse(RESP_EVENTS))
    p = responses_provider(s)
    ev = await collect(p, REQ)
    for x in show(ev):
        print("  ", x)
    # check part joins equal final
    final = ev[-1].response
    joined = {}
    for e in ev:
        if isinstance(e, (TextDelta, ReasoningDelta)):
            joined[e.part] = joined.get(e.part, "") + e.text
    print("   joins match:", [joined.get(i, "") for i in range(len(final.message.content))] == [p_.text for p_ in final.message.content])
    print("   native[0] keys:", sorted(final.native_items[0].data))
    print("   request body keys:", sorted(s.bodies[0]), "stream:", s.bodies[0].get("stream"), "reasoning:", s.bodies[0].get("reasoning"))

    print("=== Responses: empty stream (JSON body) -> fallback to non-stream, then stays non-stream")
    s = Script(oa_json(RESP_FINAL), oa_json(RESP_FINAL), oa_json(RESP_FINAL))
    p = responses_provider(s)
    ev = await collect(p, REQ)
    print("   events:", [type(e).__name__ for e in ev], "streaming flag:", p.streaming, "bodies stream flags:", [b.get("stream") for b in s.bodies])
    ev = await collect(p, REQ)
    print("   2nd call bodies stream flags:", [b.get("stream") for b in s.bodies])

    print("=== Responses: 400 param=stream -> fallback")
    err = {"error": {"message": "Your organization must be verified to stream this model", "type": "invalid_request_error", "param": "stream", "code": "unsupported_value"}}
    s = Script(oa_json(err, 400), oa_json(RESP_FINAL))
    p = responses_provider(s)
    ev = await collect(p, REQ)
    print("   events:", [type(e).__name__ for e in ev], "streaming:", p.streaming)

    print("=== Responses: 400 'upstream error' -> no fallback")
    err2 = {"error": {"message": "upstream error", "type": "invalid_request_error", "param": None}}
    s = Script(oa_json(err2, 400))
    p = responses_provider(s)
    try:
        await collect(p, REQ)
    except Exception as e:
        print("   raised:", type(e).__name__, e.status_code, "streaming:", p.streaming)

    print("=== Responses: refusal, then non-stream fails with 503 -> streaming flag stays True")
    s = Script(oa_json(err, 400), oa_json({"error": {"message": "busy"}}, 503))
    p = responses_provider(s)
    try:
        await collect(p, REQ)
    except Exception as e:
        print("   raised:", type(e).__name__, getattr(e, "status_code", None), "streaming:", p.streaming)

    print("=== Responses: non-stream gets SSE text (after refusal)")
    s = Script(oa_json(err, 400), oa_sse(RESP_EVENTS))
    p = responses_provider(s)
    try:
        await collect(p, REQ)
    except Exception as e:
        print("   raised:", type(e).__name__, str(e)[:120])

    print("=== Responses: response.failed / error event / stream ends without completed")
    for name, evs in [
        ("failed", RESP_EVENTS[:3] + [{"type": "response.failed", "sequence_number": 9, "response": {**RESP_FINAL, "status": "failed", "error": {"code": "server_error", "message": "boom"}}}]),
        ("error", [{"type": "error", "code": "x", "message": "bad thing", "param": None, "sequence_number": 1}]),
        ("no completed", RESP_EVENTS[:5]),
        ("only created", RESP_EVENTS[:1]),
        ("sse with error key", [{"error": {"message": "relay broke"}}]),
    ]:
        s = Script(oa_sse(evs))
        p = responses_provider(s)
        got = []
        try:
            async for e in p.stream(REQ):
                got.append(type(e).__name__)
        except Exception as e:
            print(f"   {name}: yielded {len(got)} then {type(e).__name__}: {str(e)[:80]}")

    print("=== Responses: incomplete -> length")
    inc = {**RESP_FINAL, "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    s = Script(oa_sse([{"type": "response.incomplete", "sequence_number": 1, "response": inc}]))
    p = responses_provider(s)
    ev = await collect(p, REQ)
    print("   finish:", ev[-1].response.finish_reason, "tool_calls present:", bool(ev[-1].response.message.tool_calls))
    print("=== Responses non-stream status failed (after fallback) ->")
    s = Script(oa_json(err, 400), oa_json({**RESP_FINAL, "status": "failed", "error": {"code": "server_error", "message": "boom"}, "output": []}))
    p = responses_provider(s)
    ev = await collect(p, REQ)
    print("   finish:", ev[-1].response.finish_reason, "content:", ev[-1].response.message.content)

    print("=== Chat: normal stream")
    s = Script(oa_sse(CHAT_EVENTS, named=False))
    p = chat_provider(s)
    ev = await collect(p, REQ)
    for x in show(ev):
        print("  ", x)
    print("   native:", ev[-1].response.native_items[0].data)
    print("   body stream_options:", s.bodies[0].get("stream_options"), "keys:", sorted(s.bodies[0]))
    print("=== Chat: stream without finish_reason")
    s = Script(oa_sse([chunk({"content": "cut"}), "data: [DONE]\n\n"], named=False))
    p = chat_provider(s)
    ev = await collect(p, REQ)
    print("   finish:", ev[-1].response.finish_reason, "usage:", ev[-1].response.usage.model_dump())
    print("=== Chat: non-stream (after empty stream)")
    s = Script(oa_json(CHAT_FINAL), oa_json(CHAT_FINAL))
    p = chat_provider(s)
    ev = await collect(p, REQ)
    print("  ", show(ev))
    print("=== Chat: non-stream choices empty")
    s = Script(oa_json(CHAT_FINAL), oa_json({**CHAT_FINAL, "choices": []}))
    p = chat_provider(s)
    try:
        await collect(p, REQ)
    except Exception as e:
        print("   raised:", type(e).__name__, e)
    print("=== Chat: 400 param=stream_options -> fallback")
    s = Script(oa_json({"error": {"message": "unknown field", "param": "stream_options"}}, 400), oa_json(CHAT_FINAL))
    p = chat_provider(s)
    ev = await collect(p, REQ)
    print("   streaming:", p.streaming, "events:", [type(e).__name__ for e in ev])

    print("=== Anthropic: normal stream")
    s = Script(an_sse(AN_EVENTS))
    p = anthropic_provider(s)
    ev = await collect(p, REQ)
    for x in show(ev):
        print("  ", x)
    print("   native:", [n.data for n in ev[-1].response.native_items])
    print("=== Anthropic: JSON body for stream request -> fallback")
    s = Script(an_json(AN_FINAL), an_json(AN_FINAL))
    p = anthropic_provider(s)
    ev = await collect(p, REQ)
    print("   streaming:", p.streaming, [type(e).__name__ for e in ev], "usage:", ev[-1].response.usage.model_dump())
    print("=== Anthropic: mid-stream error event (overloaded) before any delta")
    s = Script(an_sse(AN_EVENTS[:2] + [{"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}]))
    p = anthropic_provider(s)
    try:
        await collect(p, REQ)
    except Exception as e:
        print("   raised:", type(e).__name__, "status:", getattr(e, "status_code", None))
    print("=== Anthropic: 400 mentioning streaming -> fallback")
    s = Script(an_json({"type": "error", "error": {"type": "invalid_request_error", "message": "streaming is not supported"}}, 400), an_json(AN_FINAL))
    p = anthropic_provider(s)
    ev = await collect(p, REQ)
    print("   streaming:", p.streaming)
    print("=== Anthropic: final message no content and no stop_reason (non-stream)")
    s = Script(an_json(AN_FINAL), an_json({**AN_MESSAGE}))
    p = anthropic_provider(s)
    try:
        await collect(p, REQ)
    except Exception as e:
        print("   raised:", type(e).__name__, e)

asyncio.run(main())
