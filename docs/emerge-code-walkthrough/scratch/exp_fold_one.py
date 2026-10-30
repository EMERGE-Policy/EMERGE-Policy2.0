import asyncio
from pathlib import Path
from fake import FakeProvider, client_for, dump_history, reply
from emerge.agent import (AgentLoop, CompactionMiddleware, ContextBuilder, History, MiddlewareChain,
                          RequestBuilder, RunContext, Toolset)
from emerge.models.types import Message, TextPart


async def main():
    provider = FakeProvider([reply("never")], delay=0.3)
    history = History()
    for i, size in enumerate((700, 20)):
        history.start_turn(Message.user(f"user {i} " + " ".join(["word"] * size)))
        history.append_messages_to_current_turn(Message(role="assistant", content=[TextPart(text=f"answer {i}")]))
    ctxb = ContextBuilder(["S"]); builder = RequestBuilder(ctxb, history, [])
    client = client_for(provider)
    loop = AgentLoop(client, Toolset([]), builder, history,
                     MiddlewareChain([CompactionMiddleware(client, ctxb, history, builder, max_context_tokens=1000)]))
    t = asyncio.create_task(loop.run_turn("current", RunContext.create(Path("."))))
    await asyncio.sleep(0.45); t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    print("folded exactly one turn, then cancel → summary calls:", len(provider.summary_requests), dump_history(history))

asyncio.run(main())
