"""E1 forget race / summary; E4 no-op patch; E8 paused run going idle; E14 idle after reopen."""

from __future__ import annotations

import asyncio

from env import Env

from emerge.graph import GraphPatch


async def e1_forget_race() -> None:
    async with Env("e1") as env:
        graph = env.builder().add_node("s", "step", params={"duration": 0.2}).build()
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_state(run.run_id, "s", "finished")
        await asyncio.sleep(0.5)
        print("E1 summary before close:", repr(env.service.summary(run.run_id)))
        await env.service.close_run(run.run_id)
        print("E1 view right after close_run:", run.run_id in env.service.feedback._runs)
        await asyncio.sleep(0.3)
        view = env.service.feedback._runs.get(run.run_id)
        print("E1 view 0.3 s after close_run:", view is not None, view and view.status, view and dict(view.nodes))
        print("E1 summary after close:", repr(env.service.summary(run.run_id)))
        print("E1 summary of unknown run:", repr(env.service.summary("nope")))


async def e4_noop_patch() -> None:
    async with Env("e4") as env:
        graph = env.builder().add_node("v", "verdict", params={"duration": 5}, description="d").build()
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_state(run.run_id, "v", "running")
        result = await env.service.apply_patch(run.run_id, GraphPatch.set_node_description("v", "d"))
        print("E4 same description:", result.committed, result.changed, result.previous_version, "->", result.version, "diff empty:", not result.diff)
        print("E4 describe:", repr(result.describe()))
        patched = [e for e in env.events("graph.patched", run.run_id)]
        await asyncio.sleep(0.2)
        patched = [e for e in env.events("graph.patched", run.run_id)]
        print("E4 graph.patched events:", [(e.payload["previous_version"], e.payload["version"]) for e in patched])
        empty = await env.service.apply_patch(run.run_id, GraphPatch())
        print("E4 empty patch:", empty.committed, empty.changed, empty.version)
        await env.service.end_run(run.run_id)


async def e8_paused_idle() -> None:
    async with Env("e8") as env:
        graph = env.builder().add_node("b", "busy", params={"duration": 1.0}).build()
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_state(run.run_id, "b", "running")
        paused = await env.service.pause(run.run_id)
        print("E8 after pause:", paused.status, paused.paused, "node:", env.sup(run.run_id).state("b").state)
        await env.wait_state(run.run_id, "b", "finished", timeout=10)
        await asyncio.sleep(0.8)
        snap = env.service.runs()[0]
        print("E8 run after b finished:", snap.status, snap.paused)
        print("E8 run.idle events:", len(env.events("run.idle", run.run_id)))
        print("E8 feedback kinds:", [f.kind for f in env.feedback])
        resumed = await env.service.resume(run.run_id)
        print("E8 after resume:", resumed.status, resumed.paused)
        await env.service.end_run(run.run_id)


async def e14_idle_after_reopen() -> None:
    async with Env("e14") as env:
        graph = env.builder().add_node("s", "step", params={"duration": 0.1}).build()
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_for(lambda: any(f.kind == "graph_idle" for f in env.feedback))
        print("E14 before close:", env.service.runs()[0].status, "run.idle:", len(env.events("run.idle", run.run_id)))
        await env.service.close_run(run.run_id)
        before = len(env.events("run.idle", run.run_id))
        fb_before = len(env.feedback)
        report = await env.service.resume_run(run.run_id)
        await asyncio.sleep(1.0)
        print("E14 report status:", report.status, "kept:", report.kept)
        print("E14 run.idle after reopen:", len(env.events("run.idle", run.run_id)) - before)
        print("E14 feedback after reopen:", [f.kind for f in env.feedback[fb_before:]])
        statuses = [(e.payload["previous"], e.payload["status"]) for e in env.events("run.status", run.run_id)]
        print("E14 run.status events:", statuses)


async def main() -> None:
    await e1_forget_race()
    await e4_noop_patch()
    await e8_paused_idle()
    await e14_idle_after_reopen()


if __name__ == "__main__":
    asyncio.run(main())
