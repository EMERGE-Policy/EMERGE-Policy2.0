import asyncio
import json
from pathlib import Path
from typing import TextIO

from emerge.callbacks.base import CallbackHandler
from emerge.events import Event, EventType

SESSION_DIR = "_session"
TRACE_FILE = "trace.jsonl"


class JsonlTracer(CallbackHandler):
    """Appends every event as one JSON line to <runs_dir>/<run_id>/trace.jsonl."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)
        self._files: dict[str, TextIO] = {}

    async def on_event(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        await asyncio.to_thread(self._write, event.run_id or SESSION_DIR, line, event.type == EventType.RUN_FINISHED)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_all)

    def _write(self, key: str, line: str, finished: bool) -> None:
        file = self._files.get(key)
        if file is None:
            path = self.runs_dir / key / TRACE_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            file = self._files[key] = path.open("a", encoding="utf-8")
        file.write(line + "\n")
        file.flush()
        if finished:
            self._files.pop(key).close()

    def _close_all(self) -> None:
        for file in self._files.values():
            file.close()
        self._files.clear()
