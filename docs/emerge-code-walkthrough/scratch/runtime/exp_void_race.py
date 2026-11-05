"""E8: MessageBus.void_asks racing answer_ask on a second question of the same instance (no processes)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from emerge.runtime.bus import MessageBus


class FakeStorage:
    def __init__(self) -> None:
        self.n = 0

    async def next_content_time(self, run_id: str) -> int:
        self.n += 1
        return self.n

    async def answer_ask(self, ask_id: str, answer: str) -> str:
        return f"answered {ask_id}"

    async def void_ask(self, ask_id: str) -> str:
        await asyncio.sleep(0.05)  # a storage write that takes a moment
        return f"voided {ask_id}"


class FakeCallbacks:
    def emit_nowait(self, event) -> None:
        pass


async def main() -> None:
    bus = MessageBus("run", FakeStorage(), FakeCallbacks(), port_buffer=8)
    proc = SimpleNamespace(node_id="n", instance_id="n@v0", acts_on_environment=False, send=lambda m: None)
    bus._asks = {"a1": (proc, 1), "a2": (proc, 2)}

    async def answer_later() -> None:
        await asyncio.sleep(0.01)  # while void_asks awaits the first void_ask
        print("answer_ask ->", await bus.answer_ask("a2", "yes"))

    results = await asyncio.gather(bus.void_asks("n@v0"), answer_later(), return_exceptions=True)
    print("results:", [type(r).__name__ if isinstance(r, BaseException) else r for r in results])
    print("void_asks raised:", repr(results[0]) if isinstance(results[0], BaseException) else None)


if __name__ == "__main__":
    asyncio.run(main())
