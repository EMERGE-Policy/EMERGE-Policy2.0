"""Scratch: attach() failing half way (an extra tool named like a built-in one)."""
import asyncio, sys
from pathlib import Path

P8 = Path("/data/yuqingchi/tmp/emerge-phase-8")
sys.path.insert(0, str(P8))
import harness8  # noqa: E402
harness8.WORK = Path("/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/agent/work")
from harness8 import Env8  # noqa: E402
from fake_model import Script  # noqa: E402
from emerge.agent import Session, tool  # noqa: E402


@tool(name="read_file")
def dup() -> str:
    """A tool whose name clashes with a built-in one."""
    return "x"


async def main():
    async with Env8("e6", script=Script(), extra_tools=(dup,)) as env:
        agent = env.agent
        try:
            await Session.create(agent, title="t")
        except Exception as e:
            print("create failed:", type(e).__name__, e)
        print("after failure: agent.session set?", agent.session is not None, "| status:", agent.status)
        await agent.detach()
        print("after detach(): agent.session set?", agent.session is not None, "| status:", agent.status)
        try:
            await Session.create(agent, title="t2")
        except Exception as e:
            print("second create failed:", type(e).__name__, e)
        print("sessions stored:", [(s.title, s.status) for s in await Session.list(env.storage)])
        env.session = None


if __name__ == "__main__":
    asyncio.run(main())
