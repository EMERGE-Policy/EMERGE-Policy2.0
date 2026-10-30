"""E18 summary after removing a pending node; E19 end_run with a node waiting for its old instance."""

from __future__ import annotations

import asyncio

from env import Env

from emerge.graph import GraphPatch


async def e18_removed_in_summary() -> None:
    async with Env("e18") as env:
        graph = (
            env.builder()
            .add_node("v", "verdict", params={"duration": 30}, description="v")
            .add_node("p", "step", after=("v",), description="p")
            .build()
        )
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_state(run.run_id, "v", "running")
        await env.service.apply_patch(run.run_id, GraphPatch.remove_node("p"))
        await asyncio.sleep(0.5)
        print("E18 graph nodes:", env.service.graph(run.run_id).node_ids())
        print("E18 summary:\n" + env.service.summary(run.run_id))
        print("E18 stale events for p:", [(e.type, e.payload) for e in env.collector.events if e.node_id == "p" and e.type == "node.stale"])
        await env.service.end_run(run.run_id)


async def e19_end_run_held() -> None:
    async with Env("e19", stop_timeout=3.0) as env:
        graph = env.builder().add_node("x", "stubborn", description="x").build()
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_state(run.run_id, "x", "running")
        await env.service.apply_patch(run.run_id, GraphPatch.replace_node("x", description="x2"))
        ended = await env.service.end_run(run.run_id)
        rows = env.sup(run.run_id).instance_states()
        print("E19 end_run:", ended.status, [(r.instance_id, str(r.state), r.error) for r in rows])


async def main() -> None:
    await e18_removed_in_summary()
    await e19_end_run_held()


if __name__ == "__main__":
    asyncio.run(main())
