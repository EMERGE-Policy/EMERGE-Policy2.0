"""E6: ctx.aclose() raising after a declared outcome. E7: sync work between permission checks, then stop."""

from __future__ import annotations

import asyncio
import time

from wt_harness import Env


async def e6() -> None:
    async with Env("e6") as env:
        await env.supervisor.start(env.builder().add_node("b", "wt_bad_close").build(), env.run_id)
        st = await env.wait_state("b", {"finished", "failed", "stopped"})
        print("E6", st, env.supervisor.state("b").error)


async def e7() -> None:
    async with Env("e7", stop_timeout=3.0) as env:
        await env.supervisor.start(env.builder().add_node("s", "wt_sync_loop").build(), env.run_id)
        await env.wait_state("s", "running")
        await asyncio.sleep(0.5)
        t0 = time.monotonic()
        rec = await env.supervisor.stop("s")
        inst = env.supervisor.graph.node("s").instance_id
        teardown = (env.workspace / "runs" / env.run_id / "nodes" / "s" / inst / "teardown.json").exists()
        print("E7", rec.state, repr(rec.error), "after", round(time.monotonic() - t0, 2), "s; teardown ran:", teardown)


async def main() -> None:
    await e6()
    await e7()


if __name__ == "__main__":
    asyncio.run(main())
