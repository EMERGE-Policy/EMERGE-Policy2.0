import asyncio
from harness import *  # noqa
from emerge.models import Message, ModelRequest, ModelClient, RetryPolicy


async def main():
    for name, mk, extra in [
        ("anthropic extra prompt_cache_key", lambda s: anthropic_provider(s), {"prompt_cache_key": "k"}),
        ("anthropic extra temperature", lambda s: anthropic_provider(s), {"temperature": 0.5}),
        ("responses extra unknown", lambda s: responses_provider(s), {"foo": 1}),
        ("responses extra stream", lambda s: responses_provider(s), {"stream": False}),
        ("responses extra extra_body", lambda s: responses_provider(s), {"extra_body": {"foo": 1}}),
    ]:
        s = Script(oa_sse(RESP_EVENTS) if "responses" in name else an_sse(AN_EVENTS))
        c = ModelClient(mk(s), retry=RetryPolicy(max_retries=0))
        try:
            await c.generate(ModelRequest(messages=[Message.user("x")], extra=extra))
            print(name, "-> ok; body extra keys:", {k: s.bodies[0].get(k) for k in ("foo", "prompt_cache_key", "temperature")})
        except Exception as e:
            print(name, "->", type(e).__name__, str(e)[:100])

asyncio.run(main())
