"""Node processes: what one starts from, the main process's handle on it, and its entry point.

A node process is a fresh interpreter (multiprocessing "spawn"), so it is handed no objects: it gets
a JSON-ready `NodeLaunch`, imports the node class again from the registry's locator, and talks to
the bus over one pipe. It runs setup, run and teardown, reports how run() ended, and exits. How that
report becomes a lifecycle state is the supervisor's decision, because only the main process knows
whether a stop had been requested.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import multiprocessing
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

from emerge.exceptions import NodeError, StopRequested
from emerge.graph.node import Node, Outcome
from emerge.graph.registry import NodeRegistry
from emerge.runtime.bus import Channel, Kind
from emerge.runtime.context import RuntimeNodeContext

logger = logging.getLogger(__name__)

# Inside the instance's artifacts directory: its prints, its log records, and its tracebacks.
LOG_FILE = "node.log"


@dataclass(frozen=True, slots=True)
class NodeLaunch:
    """Everything a node process starts from. JSON-ready: it crosses into a new interpreter."""

    run_id: str
    node_id: str
    instance_id: str
    node_type: str
    # NodeLocator.to_dict() of the implementation this instance is pinned to.
    locator: dict[str, Any]
    params: dict[str, Any]
    description: str
    # Every declared input -> {"source": "node.port" | None, "closed": bool, "start": int | None} at
    # launch; later changes arrive as INPUT messages.
    inputs: dict[str, dict[str, Any]]
    # The workspace, and the folder of the conversation the run belongs to.
    workspace: str
    session_dir: str
    # WorkspaceConfig.model_dump(mode="json"); the workspace path travels separately, as above.
    config: dict[str, Any]
    port_buffer: int
    # Start paused: setup() runs as usual, and run() holds at its first `wait_for_run_permission()`
    # until a resume arrives.
    paused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeLaunch:
        return cls(**data)


class NodeProcess:
    """The main process's handle on one node process."""

    def __init__(self, launch: NodeLaunch, *, acts_on_environment: bool) -> None:
        self.launch = launch
        self.node_id = launch.node_id
        self.instance_id = launch.instance_id
        self.acts_on_environment = acts_on_environment
        # Set by the supervisor. Once a stop was requested the instance ends stopped whatever run()
        # does; `forced` means the stop timed out and the process was killed.
        self.stop_requested = False
        self.forced = False
        # What the process said about its own end (an EXIT message), if it got to say it.
        self.exit_report: dict[str, Any] | None = None
        # The process is gone and its pipe released.
        self.exited = asyncio.Event()
        self._process: BaseProcess | None = None
        self._channel: Channel | None = None

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def exitcode(self) -> int | None:
        return None if self._process is None else self._process.exitcode

    async def start(self, on_message: Callable[[NodeProcess, dict[str, Any]], None]) -> None:
        context = multiprocessing.get_context("spawn")
        parent_end, child_end = context.Pipe(duplex=True)
        process = context.Process(
            target=node_process_main,
            args=(child_end, self.launch.to_dict()),
            name=f"emerge-node:{self.instance_id}",
        )
        try:
            await asyncio.to_thread(process.start)
        except BaseException:
            parent_end.close()
            raise
        finally:
            # The child has its own copy now. Keeping this one open would hide the child's exit:
            # the pipe only reports end-of-file once every copy of the child's end is closed.
            child_end.close()
        self._process = process
        self._channel = Channel(
            parent_end, asyncio.get_running_loop(), lambda message: on_message(self, message), name=self.instance_id
        )
        self._channel.start()

    def send(self, message: dict[str, Any]) -> None:
        if self._channel is not None and not self.exited.is_set():
            self._channel.send(message)

    def control(self, op: str) -> None:
        self.send({"kind": Kind.CONTROL, "op": op})

    def kill(self) -> None:
        self.forced = True
        if self._process is not None:
            self._process.kill()

    async def join(self, timeout: float | None = None) -> None:
        if self._process is not None:
            await asyncio.to_thread(self._process.join, timeout)

    async def reap(self) -> None:
        """Wait for the process to be gone, release its pipe, and set `exited`."""
        await self.join()
        if self._channel is not None:
            await asyncio.to_thread(self._channel.close)
        self.exited.set()


# -- inside the node process -------------------------------------------------


def node_process_main(conn: Connection, launch: dict[str, Any]) -> None:
    """Entry point of a spawned node process."""
    # Ctrl+C in a terminal reaches the whole process group. Quitting is the main process's call,
    # and it stops the nodes through the bus.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    asyncio.run(_serve(conn, NodeLaunch.from_dict(launch)))


async def _serve(conn: Connection, launch: NodeLaunch) -> None:
    loop = asyncio.get_running_loop()
    channel = Channel(conn, loop, lambda message: ctx.receive(message), name=launch.instance_id)
    ctx = RuntimeNodeContext(launch, channel.send)
    _capture_output(ctx.artifacts_dir / LOG_FILE, channel)
    channel.start()
    report = await _run(launch, ctx, channel.send)
    channel.send({"kind": Kind.EXIT, **report})
    await asyncio.to_thread(channel.close)
    logging.shutdown()
    # Straight out once the end is reported: threads or executor jobs the node left behind must not
    # keep the process alive, and asyncio.run would wait for them.
    os._exit(0)


async def _run(launch: NodeLaunch, ctx: RuntimeNodeContext, send: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    try:
        cls = load_node_class(launch.node_type, launch.locator)
        ctx.bind(cls)
        node = cls(cls.Params.model_validate(launch.params) if cls.Params is not None else None)
    except Exception as e:
        logger.exception("cannot load node type %r for %s", launch.node_type, launch.instance_id)
        return _report("failed", error=f"{type(e).__name__}: {e}")
    result, outcome, reason, error, returned = "failed", None, "", None, None
    try:
        await node.setup(ctx)
        send({"kind": Kind.READY})
        value = await node.run(ctx)
        returned = value.name if isinstance(value, Outcome) else None
        result, outcome, reason, error = _classify(cls, value)
        if error is not None:
            logger.warning("%s: %s", launch.instance_id, error)
    except StopRequested:
        result = "stopped"
    except BaseException as e:
        # Logged with its traceback: into node.log, and through the bus into the main process's log.
        error = f"{type(e).__name__}: {e}"
        logger.exception("%s raised", launch.instance_id)
    finally:
        try:
            await node.teardown(ctx)
        except BaseException:
            # The result stands: teardown cleans up after run(), it does not decide how run() ended.
            logger.exception("teardown of %s failed", launch.instance_id)
        await ctx.aclose()
    if ctx.stop_requested and returned is not None:
        logger.info("%s returned %r after a stop was requested; it ends as stopped", launch.instance_id, returned)
    return _report(result, outcome=outcome, reason=reason, error=error, returned=returned)


def _classify(cls: type[Node], value: Any) -> tuple[str, str | None, str, str | None]:
    if isinstance(value, Outcome):
        if value.name in cls.outcomes:
            return "finished", value.name, value.reason, None
        return "failed", None, "", (
            f"run() returned outcome {value.name!r}, which {cls.name!r} does not declare {list(cls.outcomes)}"
        )
    if value is None:
        return "failed", None, "", "run() returned without an outcome"
    return "failed", None, "", f"run() returned {type(value).__name__}, not an Outcome"


def _report(result: str, **fields: Any) -> dict[str, Any]:
    # `returned`: the outcome name run() returned, declared or not, kept for the log.
    report = {"result": result, "outcome": None, "reason": "", "error": None, "returned": None}
    report.update(fields)
    return report


def load_node_class(node_type: str, locator: dict[str, Any]) -> type[Node]:
    """Import the implementation `locator` points at and return its class named `node_type`."""
    if locator["origin"] == "file":
        path = Path(locator["path"])
        # Checked before anything in the file runs: rewritten content is a different implementation,
        # and an instance pinned to this version must not run it.
        if hashlib.sha256(path.read_bytes()).hexdigest() != locator["digest"]:
            raise NodeError(
                f"{path} no longer holds version {locator['version']!r} of {node_type!r}: "
                "its content changed after that version was registered"
            )
        entries = NodeRegistry().register_from_file(path, locator["version"])
        entry = next((entry for entry in entries if entry.name == node_type), None)
        if entry is None:
            raise NodeError(f"{path} defines no node class named {node_type!r}")
        # And what actually ran is that version too, not a file rewritten in between.
        if entry.locator.digest != locator["digest"]:
            raise NodeError(f"{path} changed while it was being loaded")
        return entry.cls
    module = importlib.import_module(locator["module"])
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Node) and value.name == node_type:
            return value
    raise NodeError(f"module {locator['module']!r} defines no node class named {node_type!r}")


class _BusLogHandler(logging.Handler):
    """Warnings and errors also reach the main process's logging, tagged with the instance."""

    def __init__(self, channel: Channel) -> None:
        super().__init__(logging.WARNING)
        self._channel = channel

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self._channel.send({"kind": Kind.LOG, "level": record.levelno, "name": record.name, "message": message})


def _capture_output(path: Path, channel: Channel) -> None:
    """Send the node's prints, log records and tracebacks to its log file instead of the terminal."""
    stream = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout.flush()
    sys.stderr.flush()
    # The descriptors too: native libraries and the interpreter itself write to them directly.
    os.dup2(stream.fileno(), 1)
    os.dup2(stream.fileno(), 2)
    sys.stdout = sys.stderr = stream
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(_BusLogHandler(channel))
    root.setLevel(logging.INFO)
