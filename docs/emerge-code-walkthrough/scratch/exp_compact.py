"""Compaction: trigger, kept turns, guards, failures, cancellation after compaction."""
import asyncio
from pathlib import Path

from fake import FakeProvider, client_for, dump_history, reply
from emerge.agent import (AgentLoop, CompactionMiddleware, Compactor, ContextBuilder, History, MiddlewareChain,
                          RequestBuilder, RunContext, Toolset, tool)
from emerge.exceptions import ModelError
from emerge.models.types import Message, NativeItem, TextPart
from emerge.models.usage import estimate_tokens, _encoding

print("tiktoken available:", _encoding() is not None)


@tool
def fast(x: int) -> str:
    """Fast tool."""
    return f"fast {x}"


def big(n):  # roughly n tokens of text
    return " ".join(["word"] * n)


def seed(history, turns, size=200, thinking=False):
    for i in range(turns):
        history.start_turn(Message.user(f"user {i} " + big(size)))
        native = [NativeItem(provider="anthropic", api_mode="anthropic_messages", data={"type": "thinking", "thinking": "t", "signature": "s"}),
                  NativeItem(provider="anthropic", api_mode="anthropic_messages", data={"type": "text", "text": f"answer {i}"})] if thinking else []
        history.append_messages_to_current_turn(Message(role="assistant", content=[TextPart(text=f"answer {i}")], native_items=native))


def make(script, *, max_ctx, delay=0.0, summary="SUMMARY TEXT", runtime_info=None, sysparts=("SYSTEM v1",)):
    provider = FakeProvider(script, summary=summary, delay=delay)
    history = History()
    parts = list(sysparts)
    ctxb = ContextBuilder([lambda: parts[0]], runtime_info=runtime_info)
    tools = Toolset([fast])
    builder = RequestBuilder(ctxb, history, tools.specs())
    client = client_for(provider)
    comp = CompactionMiddleware(client, ctxb, history, builder, max_context_tokens=max_ctx)
    loop = AgentLoop(client, tools, builder, history, MiddlewareChain([comp]))
    return loop, provider, history, comp, ctxb, parts


class Listener:
    def __init__(self): self.calls = []
    def turn_started(self, turn, message): self.calls.append(("turn_started", turn, message.text[:20]))
    def messages_appended(self, turn, messages): self.calls.append(("messages_appended", turn, [m.role for m in messages]))
    def turns_dropped(self, start, stop): self.calls.append(("turns_dropped", start, stop))
    def compacted(self, summary, kept): self.calls.append(("compacted", summary.text[:45], [(i, m.role, len(m.native_items)) for i, m in kept]))


async def main():
    print("=== basic: 4 seeded turns (~200 tokens each, with thinking), budget small")
    loop, p, h, comp, ctxb, parts = make([reply("after compaction")], max_ctx=1000)
    seed(h, 4, thinking=True)
    lst = Listener(); h.set_listener(lst)
    parts[0] = "SYSTEM v2"  # e.g. MEMORY.md changed: visible only after compaction
    print("budget:", comp.compactor.budget, "keep budget:", max(1, int(comp.compactor.budget * 0.5)))
    print("sizes per turn:", [estimate_tokens(t.messages) for t in h.turns])
    ctx = RunContext.create(Path("."))
    r = await loop.run_turn("new question", ctx)
    print("result:", r.finish_reason, "usage incl. summary call:", r.usage.input_tokens, "summary calls:", len(p.summary_requests))
    for line in dump_history(h): print("  ", line[:150])
    print("native items left per message:", [[len(m.native_items) for m in t.messages] for t in h.turns])
    print("system sent in main request:", p.requests[0].messages[0].text)
    print("listener:", lst.calls)
    print("measured after turn:", comp.compactor._measured)
    print("summary request (first 300 chars of transcript part):", p.summary_requests[0].messages[0].text.split("Conversation to summarize:")[1][:300])

    print("=== only 1 turn in history (huge) → no compaction")
    loop, p, h, comp, ctxb, parts = make([reply("ok")], max_ctx=100)
    r = await loop.run_turn("x " + big(500), RunContext.create(Path(".")))
    print("summary calls:", len(p.summary_requests), "turns:", len(h.turns))

    print("=== guard: [summary turn, huge newest turn] → no compaction")
    loop, p, h, comp, ctxb, parts = make([reply("ok")], max_ctx=1000)
    seed(h, 2)
    h.compact(1, "old summary")
    print("before:", dump_history(h)[0][:60], "turns:", len(h.turns))
    r = await loop.run_turn("newest " + big(900), RunContext.create(Path(".")))
    print("summary calls:", len(p.summary_requests), "turns:", len(h.turns), [t.summary for t in h.turns])

    print("=== all turns fit in keep budget but request over budget (big system) → oldest turn folded")
    loop, p, h, comp, ctxb, parts = make([reply("ok")], max_ctx=1000, sysparts=("SYSTEM " + big(900),))
    seed(h, 3, size=20)
    r = await loop.run_turn("q", RunContext.create(Path(".")))
    print("summary calls:", len(p.summary_requests), dump_history(h))

    print("=== summary comes back empty → no compaction, over-budget request is sent")
    loop, p, h, comp, ctxb, parts = make([reply("ok")], max_ctx=1000, summary="   ")
    seed(h, 4)
    r = await loop.run_turn("q", RunContext.create(Path(".")))
    print("summary calls:", len(p.summary_requests), "turns:", len(h.turns), "result:", r.finish_reason)

    print("=== summary call raises ModelError → the turn fails")
    loop, p, h, comp, ctxb, parts = make([reply("ok")], max_ctx=1000, summary=ModelError("summary 500"))
    seed(h, 4)
    try:
        await loop.run_turn("q", RunContext.create(Path(".")))
    except ModelError as e:
        print("ModelError:", e, "turns:", len(h.turns), "last turn:", dump_history(h)[-1][:80])

    print("=== cancel the main call AFTER compaction in the same turn")
    loop, p, h, comp, ctxb, parts = make([reply("never")], max_ctx=1000, delay=0.3)
    seed(h, 4)
    lst = Listener(); h.set_listener(lst)
    t = asyncio.create_task(loop.run_turn("cancel me", RunContext.create(Path("."))))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if p.summary_requests and loop._inflight is not None and h.turns[0].summary:
            break
    await asyncio.sleep(0.05)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    print("history after cancel:", dump_history(h))
    print("listener calls after compaction:", [c for c in lst.calls if c[0] != "compacted"][-2:])
    print("stale measured pair:", comp.compactor._measured, "(sent messages now:", len(RequestBuilder(ctxb, h, []).build().messages), ")")

    print("=== checkpoint returns False before a tool, after compaction in the same turn")
    loop, p, h, comp, ctxb, parts = make([reply("", [("c1", "fast", {"x": 1})])], max_ctx=1000)
    seed(h, 4)
    n = {"n": 0}
    async def hook(c):
        n["n"] += 1
        return False if n["n"] == 2 else None
    r = await loop.run_turn("q", RunContext.create(Path(".")), checkpoint_hook=hook)
    print("result:", r.finish_reason, dump_history(h))

    print("=== runtime info unchanged, compaction in iteration 1 → appended again in iteration 2")
    loop, p, h, comp, ctxb, parts = make([reply("", [("c1", "fast", {"x": 1})]), reply("done")], max_ctx=1000, runtime_info=lambda: "RUN INFO v1")
    seed(h, 4)
    r = await loop.run_turn("q", RunContext.create(Path(".")))
    print("result:", r.finish_reason, dump_history(h)[-1])

    print("=== exact count: estimate = measured + tail")
    c = Compactor(client_for(FakeProvider()), ContextBuilder(["S"]), max_context_tokens=1000)
    hist = History(); seed(hist, 2, size=10)
    req = RequestBuilder(ContextBuilder(["S"]), hist, []).build()
    print("no measurement → full estimate:", c.estimate_input_tokens(req))
    c.record_usage(len(req.messages), 700)
    hist.append_messages_to_current_turn(Message.user("tail " + big(10)))
    req2 = RequestBuilder(ContextBuilder(["S"]), hist, []).build()
    print("measured 700 + tail:", c.estimate_input_tokens(req2), "tail alone:", estimate_tokens(req2.messages[len(req.messages):]))
    c.record_usage(5, 0)
    print("record_usage with 0 tokens ignored → measured still", c._measured)
    shorter = RequestBuilder(ContextBuilder(["S"]), History(), []).build()
    print("request shorter than measured → full estimate:", c.estimate_input_tokens(shorter))

asyncio.run(main())
