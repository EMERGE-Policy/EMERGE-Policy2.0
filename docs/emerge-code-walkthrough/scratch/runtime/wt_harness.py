"""Minimal environment for the walkthrough experiments, modelled on the phase 6 harness."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from emerge.callbacks import CallbackHandler, CallbackManager  # noqa: E402
from emerge.config import WorkspaceConfig  # noqa: E402
from emerge.events import Event  # noqa: E402
from emerge.graph import GraphBuilder, NodeLocator, NodeRegistry  # noqa: E402
from emerge.runtime import Supervisor  # noqa: E402
from emerge.storage import open_storage  # noqa: E402

import wt_nodes  # noqa: E402

WORK = HERE / "work"


class Collector(CallbackHandler):
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def on_event(self, event: Event) -> None:
        self.events.append(event)


def make_config(workspace: Path, stop_timeout: float) -> WorkspaceConfig:
    config = WorkspaceConfig.model_validate(
        {
            "providers": {"openai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "unused"}},
            "model": {"provider": "openai", "api_mode": "responses", "name": "unused", "limit": {"context": 200000, "output": 32000}},
            "runtime": {"stop_timeout": stop_timeout, "port_buffer": 8},
        }
    )
    config._workspace = workspace
    return config


class Env:
    def __init__(self, name: str, stop_timeout: float = 3.0) -> None:
        self.name = name
        self.stop_timeout = stop_timeout

    async def __aenter__(self) -> "Env":
        self.workspace = WORK / self.name
        shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True)
        self.config = make_config(self.workspace, self.stop_timeout)
        self.storage = await open_storage(self.workspace, port_buffer=8)
        session = await self.storage.create_session(title=self.name)
        self.run_id = (await self.storage.create_run(session.id)).id
        self.manager = CallbackManager()
        self.collector = Collector()
        self.manager.add_handler(self.collector)
        self.registry = NodeRegistry()
        for cls in wt_nodes.NODES:
            self.registry.register(cls, NodeLocator(origin="module", module="wt_nodes"))
        self.supervisor = Supervisor(self.storage, self.manager, self.config, self.registry)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.supervisor.shutdown()
        finally:
            await self.manager.aclose()
            await self.storage.aclose()

    def builder(self) -> GraphBuilder:
        return GraphBuilder(self.registry)

    async def wait_state(self, node_id: str, states: set[str] | str, timeout: float = 30.0) -> str:
        wanted = {states} if isinstance(states, str) else states
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.supervisor.state(node_id)
            if record is not None and str(record.state) in wanted:
                return str(record.state)
            await asyncio.sleep(0.02)
        raise TimeoutError(f"{node_id} not in {wanted}")

    def events_of(self, node_id: str, type_: str | None = None) -> list[Event]:
        return [e for e in self.collector.events if e.node_id == node_id and (type_ is None or e.type == type_)]
