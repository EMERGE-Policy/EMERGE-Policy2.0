"""Checkpoint, cancellation, close_turn, continue_turn, errors."""
import asyncio
from pathlib import Path

from fake import FakeProvider, ListEmitter, client_for, dump_history, reply
from emerge.agent import (AgentLoop, AgentMiddleware, ContextBuilder, History, MiddlewareChain,
                          RequestBuilder, RunContext, RunLimits, Toolset, tool)
from emerge.exceptions import ModelError, StopRequested
from emerge.models.types import Message

started = asyncio.Event()


@tool
async def slow(x: int) -> str:
    """Slow tool."""
    started.set()
    await asyncio.sleep(0.3)
    return f"slow {x}"


@tool
def fast(x: int) -> str:
    """Fast tool."""
    return f"fast {x}"


@tool
def stopper() -> str:
    """Raises StopRequested."""
    raise StopRequested()


class Ends(AgentMiddleware):
    def __init__(self): self.results = []
    async def on_turn_end(self, ctx, result): self.results.append(result.finish_reason)


class Explode(AgentMiddleware):
    async def wrap_tool_call(self, ctx, call, handler):
        if call.name == "fast" and call.arguments.get("x") == 99:
            raise RuntimeError("middleware failure")
        return await handler(ctx, call)


def make(script, delay=0.0, limits=None, mws=(), runtime_info=None):
    provider = FakeProvider(script, delay=delay)
    history = History()
    ctxb = ContextBuilder(["SYSTEM"], runtime_info=runtime_info)
    tools = Toolset([slow, fast, stopper])
    builder = RequestBuilder(ctxb, history, tools.specs())
    ends = Ends()
    loop = AgentLoop(client_for(provider), tools, builder, history, MiddlewareChain([ends, *mws]), limits)
    return loop, provider, history, ends


def ctx():
    return RunContext.create(Path("."))


async def main():
    print("=== checkpoint appends a user message before the 2nd model call; hook returns 0 / True")
    calls = {"n": 0}
    loop, p, h, ends = make([reply("", [("c1", "fast", {"x": 1})]), reply("final")])
    hook_log = []
    async def hook(c):
        calls["n"] += 1
        hook_log.append(calls["n"])
        if calls["n"] == 3:  # 1: before model#1, 2: before tool c1, 3: before model#2
            h.append_messages_to_current_turn(Message.user("NEW USER MESSAGE"))
        return 0 if calls["n"] == 1 else True
    r = await loop.run_turn("hi", ctx(), checkpoint_hook=hook)
    print("hook calls:", hook_log, "result:", r.finish_reason, dump_history(h))
    print("2nd request last message:", p.requests[1].messages[-1].text)

    print("=== checkpoint returns False before tool → turn dropped")
    loop, p, h, ends = make([reply("x", [("c1", "fast", {"x": 1}), ("c2", "fast", {"x": 2})])])
    h.start_turn(Message.user("older turn")); h.append_messages_to_current_turn(Message(role="assistant", content=[]))
    n = {"n": 0}
    async def hook2(c):
        n["n"] += 1
        return False if n["n"] == 3 else None
    r = await loop.run_turn("hi", ctx(), checkpoint_hook=hook2)
    print("result:", r.finish_reason, "on_turn_end:", ends.results, dump_history(h))

    print("=== task.cancel() during model call")
    loop, p, h, ends = make([reply("never")], delay=0.5)
    t = asyncio.create_task(loop.run_turn("hi", ctx()))
    await asyncio.sleep(0.1); t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("CancelledError re-raised; on_turn_end:", ends.results, "history:", dump_history(h))

    print("=== task.cancel() during a tool")
    started.clear()
    loop, p, h, ends = make([reply("", [("c1", "slow", {"x": 1})]), reply("never")])
    t = asyncio.create_task(loop.run_turn("hi", ctx()))
    await started.wait(); t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("CancelledError re-raised; on_turn_end:", ends.results, "history:", dump_history(h))

    print("=== StopRequested from a tool")
    loop, p, h, ends = make([reply("", [("c1", "stopper", {})]), reply("never")])
    try:
        await loop.run_turn("hi", ctx())
    except StopRequested:
        print("StopRequested re-raised; on_turn_end:", ends.results, "history:", dump_history(h))

    print("=== close_turn during model call")
    loop, p, h, ends = make([reply("never written")], delay=0.5)
    t = asyncio.create_task(loop.run_turn("hi", ctx()))
    await asyncio.sleep(0.1); loop.close_turn()
    r = await t
    print("result:", r.finish_reason, "on_turn_end:", ends.results, "history:", dump_history(h), "_closing after:", loop._closing)

    print("=== close_turn during first of 3 tool calls")
    started.clear()
    loop, p, h, ends = make([reply("", [("c1", "slow", {"x": 1}), ("c2", "fast", {"x": 2}), ("c3", "fast", {"x": 3})]), reply("never")])
    t = asyncio.create_task(loop.run_turn("hi", ctx()))
    await started.wait(); loop.close_turn()
    r = await t
    print("result:", r.finish_reason, [c.id for c in r.tool_calls], "model calls:", len(p.requests), dump_history(h))

    print("=== close_turn during the LAST tool call")
    started.clear()
    loop, p, h, ends = make([reply("", [("c1", "slow", {"x": 1})]), reply("never")])
    t = asyncio.create_task(loop.run_turn("hi", ctx()))
    await started.wait(); loop.close_turn()
    r = await t
    print("result:", r.finish_reason, "model calls:", len(p.requests), dump_history(h))

    print("=== close_turn + task.cancel together during model call")
    loop, p, h, ends = make([reply("never")], delay=0.5)
    t = asyncio.create_task(loop.run_turn("hi", ctx()))
    await asyncio.sleep(0.1); loop.close_turn(); t.cancel()
    try:
        r = await t
        print("returned", r.finish_reason)
    except asyncio.CancelledError:
        print("CancelledError; history:", dump_history(h))

    print("=== continue_turn after a closed turn")
    r = None
    loop, p, h, ends = make([reply("", [("c1", "fast", {"x": 1}), ("c2", "fast", {"x": 2})]), reply("continued answer")])
    h.start_turn(Message.user("orig"))
    h.append_messages_to_current_turn(Message(role="assistant", tool_calls=[{"id": "c0", "name": "fast", "arguments": {"x": 0}}]),
                                      Message.tool("c0", "not executed: session closed"))
    r = await loop.continue_turn("[reopened] go on", ctx())
    print("result:", r.finish_reason, repr(r.text), dump_history(h))

    print("=== continue_turn cancelled → whole turn dropped (incl. earlier part)")
    loop, p, h, ends = make([reply("never")], delay=0.5)
    h.start_turn(Message.user("turn A")); h.append_messages_to_current_turn(Message(role="assistant", content=[{"type": "text", "text": "A done"}]))
    h.start_turn(Message.user("turn B (unfinished)"))
    t = asyncio.create_task(loop.continue_turn("continue B", ctx()))
    await asyncio.sleep(0.1); t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("history:", dump_history(h))

    print("=== continue_turn on empty history == run_turn")
    loop, p, h, ends = make([reply("ok")])
    r = await loop.continue_turn("hello", ctx())
    print(r.finish_reason, dump_history(h))

    print("=== close_turn while idle leaks into the next turn")
    loop, p, h, ends = make([reply("should be answered")])
    loop.close_turn()
    r = await loop.run_turn("next turn", ctx())
    print("next turn result:", r.finish_reason, "model calls:", len(p.requests), dump_history(h))

    print("=== ModelError from the model → propagates, no truncation, no on_turn_end")
    loop, p, h, ends = make([reply("", [("c1", "fast", {"x": 1})]), ModelError("HTTP 500 boom")])
    try:
        await loop.run_turn("hi", ctx())
    except ModelError as e:
        print("ModelError:", e, "on_turn_end:", ends.results, dump_history(h))

    print("=== middleware wrap_tool_call raises between tool calls → unanswered tool call left in history")
    loop, p, h, ends = make([reply("", [("c1", "fast", {"x": 1}), ("c2", "fast", {"x": 99}), ("c3", "fast", {"x": 3})])], mws=[Explode()])
    try:
        await loop.run_turn("hi", ctx())
    except RuntimeError as e:
        print("RuntimeError:", e, "on_turn_end:", ends.results, dump_history(h))

    print("=== runtime_info raises")
    def bad_info():
        raise ValueError("info failed")
    loop, p, h, ends = make([reply("x")], runtime_info=bad_info)
    try:
        await loop.run_turn("hi", ctx())
    except ValueError as e:
        print("ValueError:", e, "history:", dump_history(h))

    print("=== stream=False path")
    em = ListEmitter()
    provider = FakeProvider([reply("plain")])
    history = History(); ctxb = ContextBuilder(["S"]); tools = Toolset([fast])
    loop = AgentLoop(client_for(provider, em), tools, RequestBuilder(ctxb, history, tools.specs()), history, stream=False)
    r = await loop.run_turn("hi", ctx())
    print(r.finish_reason, [ (e.type, {k: v for k, v in e.payload.items() if k in ('stream','kind','text')}) for e in em.events])

asyncio.run(main())
