"""`Writer`: the one thread that writes snapshots, so the clock does not wait for the disk.

The simulation thread takes a snapshot's images and numbers while the world stands still, as copies that no
longer touch the simulator (`capture.take`), and hands them here. The writer turns them into files
(`capture.cut`: JPEG encoding, JSON, the renames that make a file whole), in the order they were handed over,
then runs what has to follow (the endings of the actions, in action.json). `quiet` is clear until both are
done; the simulation does not read action.json before. Nothing here calls the simulator.

A step that fails is logged and the next goes on: what follows a snapshot runs even when the snapshot could
not be written, so a node waiting on an action still sees it end.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

Job = Callable[[], None]


class Writer:
    """One background thread, writing snapshots in the order it is given them."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[list[Job] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._waiting = 0
        # Set while no snapshot is waiting or being written.
        self.quiet = threading.Event()
        self.quiet.set()
        self._thread = threading.Thread(target=self._run, name="robot-writer", daemon=True)
        self._thread.start()

    def snapshot(self, write: Job, then: Job | None = None) -> None:
        """Write a snapshot with `write`, then run `then`; `quiet` is clear until both are done."""
        with self._lock:
            self._waiting += 1
            self.quiet.clear()
        self._jobs.put([write] if then is None else [write, then])

    def flush(self) -> None:
        """Wait until every snapshot handed over so far is written."""
        self._jobs.join()

    def close(self) -> None:
        """Write what is left, then stop the thread. Safe twice."""
        if self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join()

    def _run(self) -> None:
        while (steps := self._jobs.get()) is not None:
            for step in steps:
                try:
                    step()
                except Exception:
                    logger.exception("writing a snapshot failed")
            with self._lock:
                self._waiting -= 1
                if self._waiting == 0:
                    self.quiet.set()
            self._jobs.task_done()
