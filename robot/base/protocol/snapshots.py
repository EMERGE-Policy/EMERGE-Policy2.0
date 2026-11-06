"""state.json and observation.json: what one tick shows the nodes of one body.

A **snapshot** is everything a node can see of the world at one tick, taken in one go while it stands
still. It has two halves, kept in two files of the body's folder:

    state        the joints, end-effector pose and gripper of the body's arms     state.json
    observation  the image and matrices of the cameras it sees, and `extra`       observation.json

Every body has a snapshot of its own for each tick, cut from the same moment of the world: only its own arms,
its own cameras and the shared ones, poses in its own frame. (The **scene** is something else: what the
simulation needs to bring the world back. Nodes never see it.)

A snapshot is published once and never changed, so nothing here takes a lock. Its files are written in an
order readers can rely on:

    1. obs/<tick>/<camera>.jpg        the images, written while the snapshot is taken
    2. obs/<tick>/state.json          the two halves, kept under the tick
       obs/<tick>/observation.json
    3. observation.json               the latest observation
    4. state.json                     the latest state, last of all

so once state.json shows a tick, every file of that tick is in place. All bodies are published before
tick.json, in the robot folder, shows the tick: it says every body has its snapshot.

Image paths are relative to the conversation folder, as every path handed out in a conversation is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robot.base.protocol.fileio import read_json, write_json
from robot.base.protocol.paths import body_dir, history_dir, observation_path, state_path, tick_path


@dataclass(frozen=True)
class ArmState:
    """One arm, as measured."""

    joint_positions: list[float]  # radians
    ee_pose: list[float]  # [x, y, z, qw, qx, qy, qz], in the body's frame, metres
    gripper: float  # 0 closed .. 1 fully open


@dataclass(frozen=True)
class State:
    """state.json: the arms of one body at `tick`."""

    tick: int
    arms: dict[str, ArmState]


@dataclass(frozen=True)
class Camera:
    """One camera: its image, and the matrices that place it."""

    image: str  # the JPEG, relative to the conversation folder
    intrinsics: list[list[float]]  # 3×3
    extrinsics: list[list[float]]  # 4×4, camera to the body's frame


@dataclass(frozen=True)
class Observation:
    """observation.json: the cameras one body sees at `tick`, and the `extra` fields it may see."""

    tick: int
    cameras: dict[str, Camera]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """The state and the observation of one body at one tick."""

    state: State
    observation: Observation

    def __post_init__(self) -> None:
        if self.state.tick != self.observation.tick:
            raise ValueError(f"a snapshot is of one tick: state {self.state.tick}, observation {self.observation.tick}")

    @property
    def tick(self) -> int:
        return self.state.tick


def publish(folder: Path, snapshot: Snapshot) -> None:
    """Write one body's snapshot, in `folder`, the body's folder, in the order above. Its images must already be written."""
    tick = snapshot.tick
    state, observation = asdict(snapshot.state), asdict(snapshot.observation)
    write_json(state_path(folder, tick), state)
    write_json(observation_path(folder, tick), observation)
    write_json(observation_path(folder), observation)
    write_json(state_path(folder), state)


def publish_all(folder: Path, snapshots: Mapping[str, Snapshot]) -> None:
    """Write every body's snapshot, in `folder`, the robot folder, then tick.json. All are of one tick."""
    ticks = {snapshot.tick for snapshot in snapshots.values()}
    if len(ticks) != 1:
        raise ValueError(f"the snapshots of one moment are of one tick, got {sorted(ticks)}")
    for body, snapshot in snapshots.items():
        publish(body_dir(folder, body), snapshot)
    write_json(tick_path(folder), {"tick": ticks.pop()})


def next_tick(folder: Path) -> int:
    """Where the ticks go on from: one past the last snapshot kept, or 0 for the first."""
    last = latest_tick(folder)
    return 0 if last is None else last + 1


def latest_tick(folder: Path) -> int | None:
    """The last tick this robot folder kept a snapshot of, in any body; None before the first."""
    ticks: list[int] = []
    marker = read_json(tick_path(folder))
    if isinstance(marker, dict) and isinstance(marker.get("tick"), int):
        ticks.append(marker["tick"])
    for body in folder.iterdir() if folder.is_dir() else ():
        history = history_dir(body)
        if history.is_dir():
            ticks.extend(int(path.name) for path in history.iterdir() if path.name.isdigit())
    return max(ticks, default=None)
