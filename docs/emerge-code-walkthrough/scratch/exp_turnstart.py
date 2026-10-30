"""Cancellation or an exception inside on_turn_start; text carried over from an earlier reply."""
import asyncio
from pathlib import Path

from fake import FakeProvider, client_for, dump_history, reply
from emerge.agent import (AgentLoop, AgentMiddleware, ContextBuilder, History, MiddlewareChain,
                          RequestBuilder, RunContext, Toolset, tool)


class SlowStart(AgentMiddleware):
    async def on_turn_start(self, ctx):
        await asyncio.sleep(0.3)


class BadStart(AgentMiddleware):
    async def on_turn_start(self, ctx):
        raise RuntimeError("on_turn_start failed")


@tool
def fast(x: int) -> str:
    """Fast."""
    return "ok"


def make(script, mws):
    provider = FakeProvider(script)
    history = History(); ctxb = ContextBuilder(["S"]); tools = Toolset([fast])
    loop = AgentLoop(client_for(provider), tools, RequestBuilder(ctxb, history, tools.specs()), history, MiddlewareChain(mws))
    return loop, history


async def main():
    loop, h = make([reply("x")], [SlowStart()])
    t = asyncio.create_task(loop.run_turn("hi", RunContext.create(Path("."))))
    await asyncio.sleep(0.1); t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("cancel during on_turn_start → history:", dump_history(h))
    loop, h = make([reply("x")], [BadStart()])
    try:
        await loop.run_turn("hi", RunContext.create(Path(".")))
    except RuntimeError as e:
        print("on_turn_start raises →", e, "history:", dump_history(h))
    loop, h = make([reply("calling now", [("c1", "fast", {"x": 1})]), reply("")], [])
    r = await loop.run_turn("hi", RunContext.create(Path(".")))
    print("last reply empty → TurnResult.text:", repr(r.text), r.finish_reason)

asyncio.run(main())
