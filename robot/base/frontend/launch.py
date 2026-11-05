"""How a conversation's simulation is started, kept in its robot folder so it can be started again.

    robot/launch.json   {"driver": "robodojo", "config": {"task": "stack_bowls"}}
    robot/first_scene   the scene the conversation began with

Recording a launch starts the conversation over: the scene saved by an earlier simulation is dropped, so
the next start begins from the new first scene. Starting again without recording carries on from the
saved scene. `SimulationProcess` runs the recorded launch as a process of its own
(`python -m robot --session <conversation>`), for a front end that owns the simulation's lifetime.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robot.base.errors import RobotError
from robot.base.protocol.fileio import read_json, write_bytes, write_json
from robot.base.protocol.paths import first_scene_path, launch_path, log_path, robot_dir, scene_path, state_path


@dataclass(frozen=True)
class Launch:
    """Which driver serves the conversation, and what it is constructed with."""

    driver: str
    config: dict[str, Any] = field(default_factory=dict)


def record_launch(session_dir: Path, launch: Launch, first_scene: bytes) -> None:
    """Make `launch`, from `first_scene`, the conversation's simulation; it starts over from that scene."""
    folder = robot_dir(session_dir)
    write_bytes(first_scene_path(folder), first_scene)
    scene_path(folder).unlink(missing_ok=True)
    write_json(launch_path(folder), {"driver": launch.driver, "config": launch.config})


def recorded_launch(session_dir: Path) -> Launch | None:
    """The conversation's recorded launch; None when it has never had a simulation."""
    record = read_json(launch_path(robot_dir(session_dir)))
    if record is None:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("driver"), str) or not isinstance(record.get("config", {}), dict):
        raise RobotError(f"{launch_path(robot_dir(session_dir))} is not a launch record")
    return Launch(record["driver"], record.get("config", {}))


class SimulationProcess:
    """The recorded simulation of one conversation, running as a process of its own.

    Its output goes to robot/simulation.log; it inherits this process's environment (CUDA_VISIBLE_DEVICES
    included). `stop()` asks it to stop as Ctrl+C would, so it fails what is unfinished and closes the
    simulator, and waits for it.
    """

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir).resolve()
        self.folder = robot_dir(self.session_dir)
        launch = recorded_launch(self.session_dir)
        if launch is None:
            raise RobotError(f"no simulation is recorded for {self.session_dir}")
        self.launch = launch
        self.started_at = time.time()
        with log_path(self.folder).open("ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "robot", "--session", str(self.session_dir)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                # Its own process group: a Ctrl+C at the terminal is the front end's to handle.
                start_new_session=True,
            )

    @property
    def status(self) -> str:
        """"starting" until its first snapshot is out, then "running"; "exited (<code>)" once it ends."""
        code = self.process.poll()
        if code is not None:
            return f"exited ({code})"
        state = state_path(self.folder)
        return "running" if state.exists() and state.stat().st_mtime >= self.started_at else "starting"

    @property
    def tick(self) -> int | None:
        state = read_json(state_path(self.folder)) if self.status == "running" else None
        return state.get("tick") if isinstance(state, dict) else None

    def stop(self, timeout: float = 60.0) -> int:
        """Stop it the way Ctrl+C does and wait; kill it when it has not ended within `timeout` seconds."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        return self.process.returncode
