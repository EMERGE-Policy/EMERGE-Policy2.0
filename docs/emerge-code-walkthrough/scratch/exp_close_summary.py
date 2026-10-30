"""close_turn while the compaction summary call is in flight; compaction folding exactly one turn then cancel."""
import asyncio
from pathlib import Path

from fake import FakeProvider, client_for, dump_history, reply
from emerge.agent import (AgentLoop, CompactionMiddleware, ContextBuilder, History, MiddlewareChain,
                          RequestBuilder, RunContext, Toolset)
from emerge.models.types import Message, TextPart


def seed(history, n, size):
    for i in range(n):
        history.start_turn(Message.user(f"user {i} " + " ".join(["word"] * size)))
        history.append_messages_to_current_turn(Message(role="assistant", content=[TextPart(text=f"answer {i}")]))


async def scenario(n_turns, size, action):
    provider = FakeProvider([reply("never")], delay=0.3)
    history = History(); seed(history, n_turns, size)
    ctxb = ContextBuilder(["S"]); builder = RequestBuilder(ctxb, history, [])
    client = client_for(provider)
    comp = CompactionMiddleware(client, ctxb, history, builder, max_context_tokens=1000)
    loop = AgentLoop(client, Toolset([]), builder, history, MiddlewareChain([comp]))
    t = asyncio.create_task(loop.run_turn("current", RunContext.create(Path("."))))
    await asyncio.sleep(0.1 if action == "close-during-summary" else 0.45)
    if action == "close-during-summary":
        loop.close_turn()
        r = await t
        print(action, "→", r.finish_reason, "summary calls:", len(provider.summary_requests), dump_history(history))
    else:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        print(action, "→ summary calls:", len(provider.summary_requests), dump_history(history))


async def main():
    await scenario(4, 200, "close-during-summary")
    await scenario(2, 330, "cancel-after-folding-one-turn")   # folds exactly 1 turn: indices unchanged
    await scenario(4, 200, "cancel-after-folding-three-turns")

asyncio.run(main())
