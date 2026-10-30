"""Experiments: E1 shutdown(closing=False) and pending nodes; E2 pause/resume during STARTING;
E3 ask after stop; E4 ctx.emitter bypass of the event type check."""

from __future__ import annotations

import asyncio
import json
import sys
import time

from wt_harness import Env


async def e1() -> None:
    async with Env("e1") as env:
        graph = env.builder().add_node("a", "wt_looper").add_node("b", "wt_step", after=["a"]).build()
        await env.supervisor.start(graph, env.run_id)
        await env.wait_state("a", "running")
        await env.supervisor.shutdown()
        rows = {r.node_id: r.state for r in await env.storage.node_states(env.run_id)}
        print("E1 after shutdown(closing=False):", rows)


async def e2() -> None:
    async with Env("e2") as env:
        graph = env.builder().add_node("s", "wt_slow_setup", params={"setup": 2.5}).build()
        await env.supervisor.start(graph, env.run_id)
        await env.wait_state("s", "starting")
        r1 = await env.supervisor.pause("s")
        r2 = await env.supervisor.resume("s")
        t_resumed = time.time()
        print("E2 pause in starting ->", r1.state, "; resume ->", r2.state, "at", round(t_resumed, 2))
        await asyncio.sleep(4.0)
        inst = graph.node("s").instance_id
        marker = env.workspace / "runs" / env.run_id / "nodes" / "s" / inst / "setup_done.json"
        print("E2 setup_done at", round(json.loads(marker.read_text())["t"], 2) if marker.exists() else None)
        print("E2 node.state sequence:", [e.payload["state"] for e in env.events_of("s", "node.state")])
        print("E2 state now:", env.supervisor.state("s").state)


async def e3() -> None:
    async with Env("e3") as env:
        graph = env.builder().add_node("q", "wt_ask_after_stop").build()
        await env.supervisor.start(graph, env.run_id)
        await env.wait_state("q", "running")
        rec = await env.supervisor.stop("q")
        await asyncio.sleep(0.3)
        asks = [e.payload for e in env.events_of("q", "node.ask")]
        rows = await env.storage.load_run_state(env.run_id)
        print("E3 final:", rec.state, rec.error, "; node.ask events:", asks)
        # all asks of the run, any status
        import sqlite3

        db = env.workspace / ".emerge" / "state.db"
        try:
            con = sqlite3.connect(db)
            print("E3 asks table:", con.execute("select status, question from asks").fetchall())
        except Exception as ex:  # table name may differ
            print("E3 asks table read failed:", ex)


async def e4() -> None:
    async with Env("e4") as env:
        graph = env.builder().add_node("x", "wt_spoofer").build()
        await env.supervisor.start(graph, env.run_id)
        await env.wait_state("x", "finished")
        for e in env.events_of("x", "node.state"):
            print("E4 node.state:", e.payload, "content_time=", e.content_time)


async def main() -> None:
    which = sys.argv[1:] or ["e1", "e2", "e3", "e4"]
    for name in which:
        await globals()[name]()


if __name__ == "__main__":
    asyncio.run(main())
