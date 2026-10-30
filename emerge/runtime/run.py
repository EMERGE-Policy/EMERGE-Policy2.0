"""A run: its record, its directory, and its status while the service holds it open.

A run's id comes from `Storage.create_run`, its directory is `runs/<run_id>/` (trace, graph file,
node artifacts). Its status follows what its nodes do: `running` while any node is live, `idle`
once every node has ended, `paused` while the whole run is paused. It does not end by itself:
an idle run takes more nodes and runs again. Only an explicit end makes it `finished` (or
`stopped`), and closing its session makes it `closed`, to be continued when the session reopens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from emerge.callbacks import EventEmitter
from emerge.events import Event
from emerge.graph.graph import Graph
from emerge.runtime.supervisor import Supervisor
from emerge.storage.artifacts import ensure_run_dir, graph_file
from emerge.storage.base import RunRecord, RunStatus, Storage

# Run events that are not in events.EventType (a phase 0 interface), like bus.NODE_STALE. Handlers
# get them through `on_event`.
# The run's status changed: payload {status, previous}.
RUN_STATUS = "run.status"
# Every node of the graph has ended: the main agent is told at once that the graph is idle.
RUN_IDLE = "run.idle"

# Statuses a run does not leave while this service holds it.
ENDED_STATUSES = frozenset({"finished", "stopped", "closed"})


class Run(BaseModel):
    """What a caller sees of a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    session_id: str
    graph_version: int
    status: RunStatus
    # The whole run is paused; `status` is then `paused` until the run ends or closes.
    paused: bool = False
    started_at: float
    finished_at: float | None = None


async def create_run(storage: Storage, workspace: Path, session_id: str) -> RunRecord:
    """A new run in Storage, which hands out its id, and its directory under `runs/`."""
    record = await storage.create_run(session_id)
    ensure_run_dir(workspace, record.id)
    return record


class RunHandle:
    """One run the service holds open: its supervisor, its status, and the lock its commits take."""

    def __init__(
        self,
        record: RunRecord,
        supervisor: Supervisor,
        storage: Storage,
        callbacks: EventEmitter,
        workspace: Path,
    ) -> None:
        self.run_id = record.id
        self.session_id = record.session_id
        self.started_at = record.started_at
        self.finished_at = record.finished_at
        self.status: RunStatus = record.status
        self.paused = record.paused
        self.graph_file_digest = record.graph_file_digest
        self.supervisor = supervisor
        self.storage = storage
        self.callbacks = callbacks
        self.workspace = workspace
        # One commit at a time per run, from a patch or from the graph file.
        self.commit_lock = asyncio.Lock()
        self._status_lock = asyncio.Lock()
        supervisor.on_idle = self.on_idle

    @property
    def graph(self) -> Graph:
        return self.supervisor.graph

    @property
    def graph_file(self) -> Path:
        return graph_file(self.workspace, self.run_id)

    @property
    def ended(self) -> bool:
        return self.status in ENDED_STATUSES

    def snapshot(self) -> Run:
        return Run(
            run_id=self.run_id,
            session_id=self.session_id,
            graph_version=self.graph.version,
            status=self.status,
            paused=self.paused,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )

    async def set_status(self, status: RunStatus) -> None:
        async with self._status_lock:
            await self._set_status(status)

    async def set_paused(self, paused: bool) -> None:
        if paused != self.paused:
            await self.storage.set_run_paused(self.run_id, paused)
            self.paused = paused
        await self.refresh_status()

    async def refresh_status(self) -> None:
        """Bring the status in line with the nodes: paused, idle, or running."""
        async with self._status_lock:
            if not self.ended:
                await self._set_status(self._live_status())

    async def reopen(self) -> None:
        """Leave `closed` for whatever the nodes say now, once the run has been carried on."""
        async with self._status_lock:
            await self._set_status(self._live_status())

    def _live_status(self) -> RunStatus:
        if self.paused:
            return "paused"
        if not self.graph.nodes or self.supervisor.idle:
            # An empty graph is idle from the start: the main agent is about to draw it.
            return "idle"
        return "running"

    async def on_idle(self) -> None:
        # Scheduled by the supervisor; a commit may have started new nodes since.
        if self.ended or not self.supervisor.idle:
            return
        self.emit(RUN_IDLE, {"graph_version": self.graph.version})
        await self.refresh_status()

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.callbacks.emit_nowait(Event(type=event_type, run_id=self.run_id, payload=payload))

    async def _set_status(self, status: RunStatus) -> None:
        if status == self.status:
            return
        previous = self.status
        record = await self.storage.set_run_status(self.run_id, status)
        self.status = status
        self.finished_at = record.finished_at
        self.emit(RUN_STATUS, {"status": status, "previous": previous})
