"""STATE.json and OBSERVATION.json: what one tick shows the nodes.

A **snapshot** is everything a node can see of the world at one tick, taken in one go while it stands
still. It has two halves, kept in two files:

    state        every arm's joints, end-effector pose and gripper     STATE.json
    observation  every camera's image and matrices, and `extra`        OBSERVATION.json

(The **scene** is something else: what the simulation needs to bring the world back. Nodes never see it.)

A snapshot is published once and never changed, so nothing here takes a lock. Its files are written in an
order readers can rely on:

    1. obs/<tick>/<camera>.jpg        the images, written while the snapshot is taken
    2. obs/<tick>/state.json          the two halves, kept under the tick
       obs/<tick>/observation.json
    3. OBSERVATION.json               the latest observation
    4. STATE.json                     the latest state, last of all

so once STATE.json shows a tick, every file of that tick is in place.

Image paths are relative to the conversation folder, as every path handed out in a conversation is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robot.base.protocol.fileio import read_json, write_json
from robot.base.protocol.paths import history_dir, observation_path, state_path


@dataclass(frozen=True)
class ArmState:
    """One arm, as measured."""

    joint_positions: list[float]  # radians
    ee_pose: list[float]  # [x, y, z, qw, qx, qy, qz], world frame, metres
    gripper: float  # 0 closed .. 1 fully open


@dataclass(frozen=True)
class State:
    """STATE.json: every arm at `tick`."""

    tick: int
    arms: dict[str, ArmState]


@dataclass(frozen=True)
class Camera:
    """One camera: its image, and the matrices that place it in the world."""

    image: str  # the JPEG, relative to the conversation folder
    intrinsics: list[list[float]]  # 3×3
    extrinsics: list[list[float]]  # 4×4, camera to world


@dataclass(frozen=True)
class Observation:
    """OBSERVATION.json: every camera at `tick`, and whatever else the driver adds."""

    tick: int
    cameras: dict[str, Camera]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """The state and the observation of one tick."""

    state: State
    observation: Observation

    def __post_init__(self) -> None:
        if self.state.tick != self.observation.tick:
            raise ValueError(f"a snapshot is of one tick: state {self.state.tick}, observation {self.observation.tick}")

    @property
    def tick(self) -> int:
        return self.state.tick


def publish(folder: Path, snapshot: Snapshot) -> None:
    """Write the snapshot's files in the order above. Its images must already be written."""
    tick = snapshot.tick
    state, observation = asdict(snapshot.state), asdict(snapshot.observation)
    write_json(state_path(folder, tick), state)
    write_json(observation_path(folder, tick), observation)
    write_json(observation_path(folder), observation)
    write_json(state_path(folder), state)


def next_tick(folder: Path) -> int:
    """Where the ticks go on from: one past the last snapshot kept, or 0 for the first."""
    last = latest_tick(folder)
    return 0 if last is None else last + 1


def latest_tick(folder: Path) -> int | None:
    """The last tick this robot folder kept a snapshot of; None before the first."""
    ticks: list[int] = []
    state = read_json(state_path(folder))
    if isinstance(state, dict) and isinstance(state.get("tick"), int):
        ticks.append(state["tick"])
    history = history_dir(folder)
    if history.is_dir():
        ticks.extend(int(path.name) for path in history.iterdir() if path.name.isdigit())
    return max(ticks, default=None)
