"""E5: a node that forks a grandchild holding the pipe, then returns."""

from __future__ import annotations

import asyncio
import time

from wt_harness import Env


async def main() -> None:
    async with Env("e5") as env:
        graph = env.builder().add_node("f", "wt_forker").build()
        await env.supervisor.start(graph, env.run_id)
        st = await env.wait_state("f", {"finished", "failed", "stopped"}); print("E5 state", st, env.supervisor.state("f"))
        t_finished = time.monotonic()
        await env.supervisor.wait_idle(30)
        print("E5 idle", round(time.monotonic() - t_finished, 2), "s after the node was recorded finished")


if __name__ == "__main__":
    asyncio.run(main())
