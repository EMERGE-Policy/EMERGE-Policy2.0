import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from emerge.callbacks.base import CallbackHandler
from emerge.events import Event, EventType


class JsonlTracer(CallbackHandler):
    """Appends every event as one JSON line to the file `path_of(event.run_id)` names.

    `path_of` gets the event's run id, or None for an event outside any run; the storage layout
    decides where each goes (`storage.artifacts.trace_file`).
    """

    def __init__(self, path_of: Callable[[str | None], Path]) -> None:
        self.path_of = path_of
        self._files: dict[str | None, TextIO] = {}

    async def on_event(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        await asyncio.to_thread(self._write, event.run_id or None, line, event.type == EventType.RUN_FINISHED)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_all)

    def _write(self, run_id: str | None, line: str, finished: bool) -> None:
        file = self._files.get(run_id)
        if file is None:
            path = self.path_of(run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            file = self._files[run_id] = path.open("a", encoding="utf-8")
        file.write(line + "\n")
        file.flush()
        if finished:
            self._files.pop(run_id).close()

    def _close_all(self) -> None:
        for file in self._files.values():
            file.close()
        self._files.clear()
