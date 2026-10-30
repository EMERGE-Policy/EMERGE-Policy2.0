"""E2/E3 stream release; E5 dry-run fate of a held node; E6 queued message count; E7 pinned code."""

from __future__ import annotations

import asyncio
import gc
import json

from env import Env, make_registry

import p7_nodes
from emerge.graph import GraphBuilder, GraphPatch
from emerge.runtime.checkpoint import load_pinned_code
from emerge.storage import write_generated_code


async def e2_e3_streams() -> None:
    async with Env("e2") as env:
        graph = env.builder().add_node("s", "step", params={"duration": 0.2}).build()
        run = await env.service.start_run(graph, env.session_id)
        sub = env.service.stream(run.run_id, ["updates"])
        print("E2 subscribers after subscribe:", env.service.streams.subscriber_count)
        del sub
        gc.collect()
        print("E2 subscribers after del + gc.collect():", env.service.streams.subscriber_count)

        sub2 = env.service.stream(run.run_id, ["debug"])

        async def consume() -> int:
            n = 0
            async for _ in sub2:
                n += 1
            return n

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.5)
        await sub2.aclose()
        await asyncio.sleep(0.5)
        print("E3 consumer finished after aclose() from another task:", task.done(),
              "subscribers:", env.service.streams.subscriber_count)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        values = env.service.stream(run.run_id, ["values"])
        first = await values.__anext__()
        print("E2 values first item:", first.type, sorted(first.payload))
        await values.aclose()
        try:
            env.service.stream(run.run_id, "updates")
        except Exception as e:  # a str is taken as a set of characters
            print("E2 modes given as a str:", type(e).__name__, e)


async def e5_dry_run_held() -> None:
    async with Env("e5", stop_timeout=4.0) as env:
        graph = env.builder().add_node("x", "stubborn", description="x").build()
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_state(run.run_id, "x", "running")
        result = await env.service.apply_patch(run.run_id, GraphPatch.replace_node("x", description="x2"))
        print("E5 replace:", result.fates, result.waiting_for_old_instance)
        path = env.workspace / "runs" / run.run_id / "graph.json"
        spec = json.loads(path.read_text())
        spec["nodes"]["y"] = {"type": "step", "description": "y", "params": {"duration": 0.1}}
        path.write_text(json.dumps(spec))
        dry = await env.service.commit_graph_file(run.run_id, dry_run=True)
        print("E5 dry run fates:", {k: str(v) for k, v in dry.fates.items()})
        real = await env.service.commit_graph_file(run.run_id)
        print("E5 real commit fates:", {k: str(v) for k, v in real.fates.items()}, "held:", real.waiting_for_old_instance)
        await env.service.end_run(run.run_id)


async def e6_queued_count() -> None:
    async with Env("e6") as env:
        graph = (
            env.builder()
            .add_node("v", "verdict", params={"duration": 30}, description="v")
            .add_node("p", "inbox", after=("v",), description="p")
            .build()
        )
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_state(run.run_id, "v", "running")
        delivery = await env.service.send_to_node(run.run_id, "p", text="hello")
        print("E6 delivery:", delivery.queued, delivery.instance_id, delivery.seq)
        result = await env.service.apply_patch(run.run_id, GraphPatch.remove_node("p"))
        print("E6 remove p:", result.committed, result.version)
        await env.service.close_run(run.run_id)
        await env.reopen()
        report = await env.service.resume_run(run.run_id)
        print("E6 report.queued_messages:", report.queued_messages)
        print("E6 describe:\n" + report.describe())
        await env.service.end_run(run.run_id)


def e7_pinned_code() -> None:
    from pathlib import Path
    import shutil

    workspace = Path(__file__).resolve().parent / "work" / "e7"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    paths = {v: write_generated_code(workspace, "gen_echo", v, p7_nodes.GEN_SOURCE.format(version=v)) for v in ("1", "2")}
    full = make_registry()
    for v, path in paths.items():
        full.register_from_file(path, v)
    graph = GraphBuilder(full).add_node("x", "gen_echo", code="1").add_node("y", "gen_echo", code="2").build()
    print("E7 pins in graph:", [(n.id, n.code) for n in graph.nodes])
    fresh = make_registry()
    print("E7 fresh registry before:", fresh.find_entry("gen_echo"))
    load_pinned_code(fresh, workspace, graph)
    entry = fresh.find_entry("gen_echo")
    print("E7 catalog current after load_pinned_code:", entry.locator.version, "versions:", fresh.versions("gen_echo"))
    print("E7 pin() for a new node without code:", fresh.pin("gen_echo"))


async def main() -> None:
    await e2_e3_streams()
    await e5_dry_run_held()
    await e6_queued_count()
    e7_pinned_code()


if __name__ == "__main__":
    asyncio.run(main())
