"""Small experiment env for the phase-7 walkthrough. Workspaces live under ./work/<name>.

Reuses (imports only) the phase-6 harness helpers and the phase-7 example nodes.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for path in (Path("/data/yuqingchi/tmp/emerge-phase-7"), Path("/data/yuqingchi/tmp/emerge-phase-6")):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from harness import Collector, make_config  # noqa: E402

import p7_nodes  # noqa: E402
from emerge.graph import GraphBuilder, NodeLocator, NodeRegistry  # noqa: E402
from emerge.runtime import RuntimeService  # noqa: E402
from emerge.storage import open_storage  # noqa: E402

WORK = HERE / "work"


def make_registry() -> NodeRegistry:
    registry = NodeRegistry()
    for cls in p7_nodes.NODES:
        registry.register(cls, NodeLocator(origin="module", module="p7_nodes"))
    return registry


class Env:
    def __init__(self, name: str, *, stop_timeout: float = 2.0, feedback_interval: float = 0.3) -> None:
        self.name = name
        self.stop_timeout = stop_timeout
        self.feedback_interval = feedback_interval

    async def __aenter__(self) -> Env:
        self.workspace = WORK / self.name
        shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True)
        self.config = make_config(self.workspace, stop_timeout=self.stop_timeout)
        await self._open()
        self.session_id = (await self.storage.create_session(title=self.name)).id
        return self

    async def _open(self) -> None:
        self.storage = await open_storage(self.workspace, port_buffer=self.config.runtime.port_buffer)
        self.registry = make_registry()
        self.feedback: list[Any] = []
        self.service = await RuntimeService.open(
            self.storage,
            self.config,
            self.registry,
            on_feedback=lambda item: self.feedback.append(item),
            feedback_interval=self.feedback_interval,
        )
        self.collector = Collector()
        self.service.callbacks.add_handler(self.collector)
        self.closed = False

    async def reopen(self) -> None:
        await self.close()
        await self._open()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.service.aclose()
        finally:
            await self.storage.aclose()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def builder(self) -> GraphBuilder:
        return GraphBuilder(self.registry)

    def sup(self, run_id: str) -> Any:
        return self.service._runs[run_id].supervisor

    async def wait_state(self, run_id: str, node_id: str, states: set[str] | str, timeout: float = 30.0) -> str:
        wanted = {states} if isinstance(states, str) else states
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.sup(run_id).state(node_id)
            if record is not None and str(record.state) in wanted:
                return str(record.state)
            await asyncio.sleep(0.02)
        raise TimeoutError(f"{node_id} not in {wanted}")

    async def wait_for(self, predicate: Any, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise TimeoutError("timed out")
            await asyncio.sleep(0.02)

    def events(self, type_: str, run_id: str | None = None) -> list[Any]:
        return [e for e in self.collector.events if e.type == type_ and (run_id is None or e.run_id == run_id)]
