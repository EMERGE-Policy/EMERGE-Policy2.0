import asyncio
import logging
import time

import httpx
import httpx2

from harness import *  # noqa
from emerge.callbacks import CallbackHandler, CallbackManager
from emerge.exceptions import ModelError, ModelAPIModeError
from emerge.models import Message, ModelClient, ModelRequest, RetryPolicy, RateLimiter

logging.basicConfig(level=logging.WARNING, format="LOG %(levelname)s %(name)s: %(message)s")
REQ = ModelRequest(messages=[Message.user("hi")])
FAST = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05)


class Rec(CallbackHandler):
    def __init__(self):
        self.events = []
    async def on_event(self, e):
        self.events.append(e)


def summarize(events):
    out = []
    for e in events:
        p = dict(e.payload)
        for k in ("call_id", "provider", "api_mode", "model"):
            p.pop(k, None)
        if "usage" in p:
            p["usage"] = {k: v for k, v in p["usage"].items() if v}
        out.append((e.type, p))
    return out


async def run(name, provider, method="generate", policy=FAST):
    m = CallbackManager(); rec = Rec(); m.add_handler(rec)
    client = ModelClient(provider, model="m", retry=policy, callbacks=m)
    t0 = time.monotonic()
    try:
        if method == "generate":
            r = await client.generate(REQ)
            res = f"ok finish={r.finish_reason}"
        else:
            n = 0
            async for _ in client.stream(REQ):
                n += 1
            res = f"ok {n} events"
    except BaseException as e:
        res = f"{type(e).__name__}: {str(e)[:110]}"
    await m.aclose()
    print(f"--- {name} [{method}] -> {res}  ({time.monotonic()-t0:.2f}s)")
    for t, p in summarize(rec.events):
        print("     ", t, p)
    return client


def boom_after(events, exc):
    """SSE that yields some events then raises a transport error."""
    async def gen():
        yield sse(events)
        raise exc
    class S(httpx.AsyncByteStream):
        async def __aiter__(self):
            async for b in gen():
                yield b
    return lambda req, body: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=S())


async def main():
    await run("responses normal", responses_provider(Script(oa_sse(RESP_EVENTS))), "generate")
    await run("responses normal", responses_provider(Script(oa_sse(RESP_EVENTS))), "stream")
    await run("fallback streamed flag", responses_provider(Script(oa_json(RESP_FINAL), oa_json(RESP_FINAL))), "generate")
    s = Script(oa_json({"error": {"message": "busy"}}, 503), oa_json({"error": {"message": "busy"}}, 503, {"retry-after": "0.01"}), oa_sse(RESP_EVENTS))
    await run("503,503(retry-after) then ok", responses_provider(s), "generate")
    s = Script(*[oa_json({"error": {"message": "busy"}}, 500)] * 3)
    await run("500 x3 (max_retries=2)", responses_provider(s), "generate")
    s = Script(oa_json({"error": {"message": "bad key"}}, 401))
    await run("401", responses_provider(s), "generate")
    s = Script(oa_json({"error": {"message": "x"}}, 409), oa_sse(RESP_EVENTS))
    await run("409 then ok", responses_provider(s), "generate")
    s = Script(oa_json({"error": {"message": "x"}}, 408), oa_sse(RESP_EVENTS))
    await run("408 then ok", responses_provider(s), "generate")
    s = Script(boom_after(RESP_EVENTS[:4], httpx.ReadTimeout("silence")), oa_sse(RESP_EVENTS))
    await run("ReadTimeout after 1 delta", responses_provider(s), "stream")
    s = Script(boom_after(RESP_EVENTS[:1], httpx.ReadTimeout("silence")), oa_sse(RESP_EVENTS))
    await run("ReadTimeout before any delta", responses_provider(s), "stream")
    s = Script(boom_after(RESP_EVENTS[:1], httpx.RemoteProtocolError("peer closed")), oa_sse(RESP_EVENTS))
    await run("RemoteProtocolError before any delta", responses_provider(s), "stream")
    s = Script(oa_sse([{"error": {"message": "relay broke"}}]))
    await run("SSE error key (APIError)", responses_provider(s), "generate")
    s = Script(an_sse(AN_EVENTS[:2] + [{"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}]), an_sse(AN_EVENTS))
    await run("anthropic in-stream overloaded", anthropic_provider(s), "generate")
    s = Script(an_json({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, 529), an_sse(AN_EVENTS))
    await run("anthropic HTTP 529 then ok", anthropic_provider(s), "generate")
    s = Script(oa_json(CHAT_FINAL), oa_json({**CHAT_FINAL, "choices": []}))
    await run("chat non-stream empty choices (IndexError)", chat_provider(s), "generate")
    # request-level speed override not supported
    from emerge.models.types import ModelRequest as MR
    p = chat_provider(Script())
    c = ModelClient(p, retry=FAST)
    try:
        await c.generate(MR(messages=[Message.user("x")], speed="ultrafast"))
    except Exception as e:
        print("--- chat speed=ultrafast per request ->", type(e).__name__, e)
    # image missing
    try:
        await c.generate(MR(messages=[Message.user("x", images=["/nonexistent.png"])]))
    except Exception as e:
        print("--- missing image ->", type(e).__name__, e)

    # probe
    for name, prov in [
        ("probe ok (stream)", responses_provider(Script(oa_sse(RESP_EVENTS)))),
        ("probe refusal -> plain", responses_provider(Script(oa_json({"error": {"message": "x", "param": "stream"}}, 400), oa_json(RESP_FINAL)))),
        ("probe empty stream -> plain", chat_provider(Script(oa_json(CHAT_FINAL), oa_json(CHAT_FINAL)))),
        ("probe 404", responses_provider(Script(oa_json({"error": {"message": "nope"}}, 404)))),
        ("probe 405", chat_provider(Script(oa_json({"error": {"message": "nope"}}, 405)))),
        ("probe 422 (not stream)", responses_provider(Script(oa_json({"error": {"message": "bad"}}, 422)))),
        ("probe 401", anthropic_provider(Script(an_json({"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}, 401)))),
        ("probe 503 x3", anthropic_provider(Script(*[an_json({"type": "error", "error": {"type": "api_error", "message": "x"}}, 503)] * 3))),
        ("probe plain returns SSE text", responses_provider(Script(oa_json({"error": {"message": "x", "param": "stream"}}, 400), oa_sse(RESP_EVENTS)))),
    ]:
        c = ModelClient(prov, retry=FAST)
        try:
            await c.probe_api_compatibility()
            print(f"--- {name} -> ok, streaming={prov.streaming}")
        except Exception as e:
            print(f"--- {name} -> {type(e).__name__}: {str(e)[:100]}")

    # limiter holds slot while consumer is slow
    lim = RateLimiter(1)
    p1 = responses_provider(Script(oa_sse(RESP_EVENTS)))
    p2 = responses_provider(Script(oa_sse(RESP_EVENTS)))
    c1, c2 = ModelClient(p1, limiter=lim), ModelClient(p2, limiter=lim)
    order = []
    async def slow_consumer():
        async for e in c1.stream(REQ):
            await asyncio.sleep(0.02)
        order.append("c1 done")
    async def second():
        await asyncio.sleep(0.01)
        await c2.generate(REQ)
        order.append("c2 done")
    await asyncio.gather(slow_consumer(), second())
    print("--- limiter order:", order)

    # consumer stops early: model.finished?
    m = CallbackManager(); rec = Rec(); m.add_handler(rec)
    c = ModelClient(responses_provider(Script(oa_sse(RESP_EVENTS))), callbacks=m)
    agen = c.stream(REQ)
    async for e in agen:
        break
    await agen.aclose()
    await m.aclose()
    print("--- consumer breaks early: events", [e.type for e in rec.events])

asyncio.run(main())
