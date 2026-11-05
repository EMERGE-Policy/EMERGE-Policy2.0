"""Headless mode: `python -m emerge`, and what the `emerge` command runs when stdin is not a terminal.

Each line on stdin is a user message, and stdout carries JSON Lines, one object per line with a `type`:
the main agent's replies and the events worth acting on. When stdin ends, the turn in progress (and any
message still queued) is finished, then the session is closed the normal way, so open runs can be
carried on later. The full-screen interface is the `tui` package, next to this one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, TextIO

from emerge.agent import AGENT_CONTROL, AGENT_FORK, AGENT_INBOX, AGENT_TURN
from emerge.application import Application, StartupError
from emerge.callbacks import CallbackHandler
from emerge.config import WorkspaceConfig
from emerge.events import Event, EventType
from emerge.runtime import RUN_IDLE, RUN_STATUS


def arguments(prog: str) -> argparse.ArgumentParser:
    """The command line both front ends take."""
    parser = argparse.ArgumentParser(prog=prog, description="EMERGE-Policy 2.0: talk to the main agent of a workspace.")
    parser.add_argument("--workspace", "-w", type=Path, default=Path("workspace"), help="workspace directory (default: ./workspace)")
    parser.add_argument("--session", "-s", help="open this session and carry it on")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = arguments("python -m emerge").parse_args(argv)
    if args.version:
        print(version_text())
        return 0
    try:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
        return asyncio.run(headless(args.workspace, args.session))
    except StartupError as e:
        print(f"emerge: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


class HeadlessOutput(CallbackHandler):
    """Writes one JSON object per line to stdout for each event a driving script would care about."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        # User messages that have entered the conversation; headless mode waits for all of them.
        self.admitted = 0

    def write(self, item: dict[str, Any]) -> None:
        self.stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.stream.flush()

    async def on_event(self, event: Event) -> None:
        kind, payload = event.type, event.payload
        if kind == EventType.AGENT_MESSAGE and payload.get("role") == "assistant":
            self.write({"type": "assistant", "text": payload.get("text", "")})
        elif kind == EventType.TOOL_STARTED:
            self.write({"type": "tool_call", "tool": payload.get("tool"), "arguments": payload.get("arguments")})
        elif kind == EventType.TOOL_FINISHED:
            self.write({"type": "tool_result", "tool": payload.get("tool"), "is_error": payload.get("is_error"), "content": _cut(payload.get("content", ""))})
        elif kind == AGENT_INBOX:
            if payload.get("kind") == "user":
                self.admitted += 1
            else:
                self.write({"type": "notice", "kind": payload.get("kind"), "text": payload.get("text")})
        elif kind == AGENT_TURN:
            self.write({"type": "turn", **{key: value for key, value in payload.items() if key != "text"}})
        elif kind == AGENT_FORK and payload.get("phase") == "answered":
            self.write({"type": "node_question", **{key: payload.get(key) for key in ("node_id", "question", "answer")}})
        elif kind == AGENT_CONTROL:
            self.write({"type": "control", **payload})
        elif kind == RUN_STATUS:
            self.write({"type": "run", "run_id": event.run_id, "status": payload.get("status")})
        elif kind == RUN_IDLE:
            self.write({"type": "run_idle", "run_id": event.run_id})


async def headless(
    workspace: Path | None,
    session_id: str | None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    config: WorkspaceConfig | None = None,
) -> int:
    """Run headless: `stdin` and `stdout` default to the process's; `config` replaces reading config.json."""
    source = stdin or sys.stdin
    output = HeadlessOutput(stdout)
    app = await Application.create(workspace, session_id=session_id, handlers=(output,), config=config)
    agent = app.agent
    try:
        output.write(
            {
                "type": "session",
                "session_id": app.session.id,
                "workspace": str(app.workspace),
                "session_dir": str(app.session_dir),
            }
        )
        submitted = 0
        while True:
            line = await asyncio.to_thread(source.readline)
            if not line:
                break
            if line.strip():
                await agent.submit(line.strip())
                submitted += 1
        # Every message read must reach the agent, and the turn handling it must end.
        while output.admitted < submitted or agent.status == "working":
            await asyncio.sleep(0.1)
    finally:
        await app.aclose()
    output.write({"type": "closed", "session_id": app.session.id if app.session else None})
    return 0


def _cut(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + f"... ({len(text) - limit} more characters)"


def version_text() -> str:
    try:
        return version("emerge")
    except PackageNotFoundError:
        return "unknown"
