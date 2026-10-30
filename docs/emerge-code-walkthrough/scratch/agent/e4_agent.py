"""Scratch: (a) interrupt in the same tick as submit; (b) a ModelError inside a turn."""
import asyncio, sys
from pathlib import Path

P8 = Path("/data/yuqingchi/tmp/emerge-phase-8")
sys.path.insert(0, str(P8))
import harness8  # noqa: E402
harness8.WORK = Path("/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/agent/work")
from harness8 import Env8  # noqa: E402
from fake_model import Script, reply  # noqa: E402
from emerge.exceptions import ModelError  # noqa: E402


async def a_interrupt_same_tick():
    print("== (a) submit, then interrupt in the same tick")
    script = Script()
    async with Env8("e4a", script=script) as env:
        session = await env.new_session()
        agent = env.agent
        await agent.submit("first message")
        control = asyncio.create_task(agent.control("interrupt"))
        result = await control
        await agent.wait_until_idle(10)
        await session.flush()
        print("control:", result)
        print("history turns:", len(session.history.turns), "| model requests:", len(script.requests))
        print("stored messages:", [r.message.text for r in await env.storage.load_messages(session.id)])
        print("pending_inputs:", [p.message.text for p in await env.storage.pending_inputs(session.id)])
        print("intent:", agent.inbox.intent, "| status:", session.status, "| finished events:", [e.payload.get("finish_reason") for e in env.turns_finished()])
        script.default = lambda request: reply("handled after reopen")
        session = await env.reopen()
        await env.agent.wait_until_idle(20)
        await session.flush()
        print("after reopen, history:", [m.text[:40] for m in session.history.flatten_messages()])
        print("after reopen, pending_inputs:", [p.message.text for p in await env.storage.pending_inputs(session.id)])
        print("after reopen, intent:", env.agent.inbox.intent)


async def b_model_error():
    print("== (b) ModelError in a turn")
    script = Script()
    async with Env8("e4b", script=script) as env:
        session = await env.new_session()
        agent = env.agent

        def boom(request):
            raise ModelError("upstream 500 after retries")

        script.add(boom)
        await agent.submit("please do it")
        await agent.wait_until_idle(10)
        await session.flush()
        fin = env.turns_finished()[-1].payload
        print("turn finished payload:", fin)
        print("history:", [(m.role, m.text[:50]) for m in session.history.flatten_messages()])
        print("stored:", [(r.turn, r.message.role, r.message.text[:50]) for r in await env.storage.load_messages(session.id)])
        print("session status:", session.status, "| agent status:", agent.status)
        script.add(reply("ok now"))
        await agent.submit("try again")
        await agent.wait_until_idle(10)
        last = script.main_requests()[-1]
        print("next request roles:", [m.role for m in last.messages])
        print("next request texts:", [m.text[:40] for m in last.messages if m.role != "system"])


async def main():
    await a_interrupt_same_tick()
    await b_model_error()


if __name__ == "__main__":
    asyncio.run(main())
