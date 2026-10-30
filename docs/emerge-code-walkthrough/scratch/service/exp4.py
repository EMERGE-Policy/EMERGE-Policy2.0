"""E20 values snapshot order; E21 resume_run failing part way."""

from __future__ import annotations

import asyncio

from env import Env


def ticker_version(event) -> int | None:
    port = event.payload.get("ports", {}).get("t.out")
    return None if port is None else port["version"]


async def e20_values_order() -> None:
    async with Env("e20") as env:
        graph = env.builder().add_node("t", "ticker", params={"interval": 0.1, "count": 8}).build()
        run = await env.service.start_run(graph, env.session_id)
        sub = env.service.stream(run.run_id, ["values"])
        await env.wait_for(lambda: sum(e.type == 'port.published' for e in env.collector.events) >= 3)
        await asyncio.sleep(0.2)  # let the stream handler catch up
        first = await sub.__anext__()
        second = await sub.__anext__()
        third = await sub.__anext__()
        print("E20 ticker.out version in 1st/2nd/3rd values item:", ticker_version(first), ticker_version(second), ticker_version(third))
        await sub.aclose()
        await env.service.end_run(run.run_id)


async def e21_resume_fails() -> None:
    async with Env("e21") as env:
        graph = (
            env.builder()
            .add_node("v", "verdict", params={"duration": 30})
            .add_node("a", "asker")
            .build()
        )
        run = await env.service.start_run(graph, env.session_id)
        await env.wait_for(lambda: any(e.type == "node.ask" for e in env.collector.events))
        await env.service.close_run(run.run_id)
        await env.reopen()
        del env.service.registry._entries["verdict"]
        try:
            await env.service.resume_run(run.run_id)
        except Exception as e:
            print("E21 resume_run raised:", type(e).__name__, str(e)[:120])
        record = await env.storage.get_run(run.run_id)
        asks = await env.storage.pending_asks(run.run_id)
        print("E21 run status after failure:", record.status, "pending asks left:", len(asks))
        print("E21 held by service:", run.run_id in env.service._runs)
        print("E21 bus tasks alive:", [t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("bus:")])


async def main() -> None:
    await e20_values_order()


if __name__ == "__main__":
    asyncio.run(main())
