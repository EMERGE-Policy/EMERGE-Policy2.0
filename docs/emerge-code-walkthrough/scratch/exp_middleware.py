"""Onion order, short-circuit, tool interception and events."""
import asyncio
from pathlib import Path

from fake import FakeProvider, ListEmitter, client_for, dump_history, reply
from emerge.agent import (AgentLoop, AgentMiddleware, ContextBuilder, History, MiddlewareChain,
                          RequestBuilder, RunContext, RunLimits, Toolset, ToolResult, tool)
from emerge.models.types import ModelResponse, Message

log = []


class Tracer(AgentMiddleware):
    def __init__(self, name, short_model=False, short_tool=False, replace_req=False):
        self.name = name
        self.short_model = short_model
        self.short_tool = short_tool
        self.replace_req = replace_req
    async def on_turn_start(self, ctx):
        log.append(f"{self.name}.on_turn_start")
    async def on_turn_end(self, ctx, result):
        log.append(f"{self.name}.on_turn_end({result.finish_reason})")
    async def before_model(self, ctx, request):
        log.append(f"{self.name}.before_model(tool_choice={request.tool_choice})")
        if self.replace_req:
            return request.model_copy(update={"tool_choice": "auto"})
        return None
    async def after_model(self, ctx, response):
        log.append(f"{self.name}.after_model")
        return None
    async def wrap_model_call(self, ctx, request, handler):
        log.append(f"{self.name}.wrap_model:pre")
        if self.short_model:
            log.append(f"{self.name}.wrap_model:SHORT")
            return reply("short-circuited")
        r = await handler(ctx, request)
        log.append(f"{self.name}.wrap_model:post")
        return r
    async def wrap_tool_call(self, ctx, call, handler):
        log.append(f"{self.name}.wrap_tool:pre({call.name})")
        if self.short_tool and call.name == "danger":
            log.append(f"{self.name}.wrap_tool:SHORT")
            return ToolResult(content="cancelled by middleware")
        r = await handler(ctx, call)
        log.append(f"{self.name}.wrap_tool:post")
        return r


@tool
def ping(x: int) -> str:
    """Ping."""
    log.append(f"ping.run({x})")
    return f"pong {x}"


@tool
def danger() -> str:
    """Danger."""
    log.append("danger.run")
    return "boom"


def make(script, mws, limits=None, emitter=None):
    provider = FakeProvider(script)
    history = History()
    ctxb = ContextBuilder(["SYSTEM"])
    tools = Toolset([ping, danger])
    builder = RequestBuilder(ctxb, history, tools.specs())
    loop = AgentLoop(client_for(provider), tools, builder, history, MiddlewareChain(mws), limits, callbacks=emitter)
    return loop, provider, history


async def main():
    print("=== onion order with [A, B]")
    loop, p, h = make([reply("", [("c1", "ping", {"x": 1})]), reply("final")], [Tracer("A", replace_req=True), Tracer("B")])
    r = await loop.run_turn("hi", RunContext.create(Path(".")))
    for line in log: print("  ", line)
    print("result:", r.finish_reason, repr(r.text), [c.name for c in r.tool_calls])

    print("=== A short-circuits wrap_model_call")
    log.clear()
    loop, p, h = make([], [Tracer("A", short_model=True), Tracer("B")])
    r = await loop.run_turn("hi", RunContext.create(Path(".")))
    for line in log: print("  ", line)
    print("provider requests:", len(p.requests), "result:", r.finish_reason, repr(r.text))

    print("=== B intercepts tool 'danger'; events; max_tool_calls counts intercepted call")
    log.clear()
    em = ListEmitter()
    loop, p, h = make([reply("calling", [("c1", "danger", {}), ("c2", "ping", {"x": 2}), ("c3", "nope", {})]), reply("after")],
                      [Tracer("A"), Tracer("B", short_tool=True)], emitter=em)
    r = await loop.run_turn("hi", RunContext.create(Path("."), run_id="R1"))
    for line in log: print("  ", line)
    print("result:", r.finish_reason, repr(r.text), [c.name for c in r.tool_calls])
    for e in em.events: print("   event", e.type, e.run_id, e.payload)
    for line in dump_history(h): print("  ", line)

    print("=== max_tool_calls=1 with 3 calls")
    log.clear()
    loop, p, h = make([reply("", [("c1", "ping", {"x": 1}), ("c2", "ping", {"x": 2}), ("c3", "ping", {"x": 3})]), reply("never")], [], RunLimits(max_tool_calls=1))
    r = await loop.run_turn("hi", RunContext.create(Path(".")))
    print("result:", r.finish_reason, [c.id for c in r.tool_calls], "model calls:", len(p.requests))
    for line in dump_history(h): print("  ", line)

    print("=== max_tool_calls=2, model makes exactly 2 calls then answers")
    loop, p, h = make([reply("", [("c1", "ping", {"x": 1}), ("c2", "ping", {"x": 2})]), reply("answer")], [], RunLimits(max_tool_calls=2))
    r = await loop.run_turn("hi", RunContext.create(Path(".")))
    print("result:", r.finish_reason, repr(r.text), "model calls:", len(p.requests))

    print("=== max_iterations=0 / max_iterations=1 with tool reply")
    loop, p, h = make([reply("x")], [Tracer("A")], RunLimits(max_iterations=0))
    log.clear()
    r = await loop.run_turn("hi", RunContext.create(Path(".")))
    print("iter=0 result:", r.finish_reason, "model calls:", len(p.requests), dump_history(h), log)
    loop, p, h = make([reply("first", [("c1", "ping", {"x": 1})]), reply("never")], [], RunLimits(max_iterations=1))
    r = await loop.run_turn("hi", RunContext.create(Path(".")))
    print("iter=1 result:", r.finish_reason, repr(r.text), "model calls:", len(p.requests), dump_history(h))

    print("=== max_input_tokens=150 (each call reports 100 input)")
    loop, p, h = make([reply("", [("c1", "ping", {"x": 1})]), reply("", [("c2", "ping", {"x": 2})]), reply("never")], [], RunLimits(max_input_tokens=150))
    r = await loop.run_turn("hi", RunContext.create(Path(".")))
    print("result:", r.finish_reason, "model calls:", len(p.requests), "usage:", r.usage.input_tokens, dump_history(h))

    print("=== RunLimits.from_config")
    print(RunLimits.from_config({"max_iterations": 3, "unknown": 1}))
    from emerge.config import RunLimits as CfgLimits
    print(RunLimits.from_config(CfgLimits(max_tool_calls=4)))

asyncio.run(main())
