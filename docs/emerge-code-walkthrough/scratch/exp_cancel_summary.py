import asyncio
from pathlib import Path
from fake import FakeProvider, client_for, dump_history, reply
from emerge.agent import (AgentLoop, CompactionMiddleware, ContextBuilder, History, MiddlewareChain,
                          RequestBuilder, RunContext, Toolset)
from emerge.models.types import Message, TextPart


async def main():
    provider = FakeProvider([reply("never")], delay=0.3)
    history = History()
    for i in range(4):
        history.start_turn(Message.user(f"user {i} " + " ".join(["word"] * 200)))
        history.append_messages_to_current_turn(Message(role="assistant", content=[TextPart(text=f"answer {i}")]))
    ctxb = ContextBuilder(["S"]); builder = RequestBuilder(ctxb, history, [])
    client = client_for(provider)
    loop = AgentLoop(client, Toolset([]), builder, history,
                     MiddlewareChain([CompactionMiddleware(client, ctxb, history, builder, max_context_tokens=1000)]))
    t = asyncio.create_task(loop.run_turn("current", RunContext.create(Path("."))))
    await asyncio.sleep(0.1); t.cancel()   # the summary call is still sleeping
    try:
        await t
    except asyncio.CancelledError:
        print("cancel during summary → turns:", len(history.turns), "summary turn present:", history.turns[0].summary, "| last:", dump_history(history)[-1][:40])

asyncio.run(main())
