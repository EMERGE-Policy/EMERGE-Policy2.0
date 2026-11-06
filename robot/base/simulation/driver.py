"""`RobotDriver`: what a simulator implements to run behind the robot folder.

`run_simulation` owns the beat: when a tick happens, when a snapshot is taken, and what the files say.
A driver only knows its simulator: how to start and stop it, how to read the arms and cameras, how to
advance one tick with every arm's current action, and how to save the whole scene to a file and bring
it back exactly. Scenes are the simulation's own business: it saves one after every tick and starts
from it, so a simulation killed and started again carries on where it was; nodes never see them.

Arms are exposed to nodes one by one. Combining them into one simulator step, for a simulator that
steps all arms at once, is the driver's job in `execute_action`.

What a driver can start from is listed by `scenes()`, without starting anything, so a front end (the TUI)
can offer the choice before a simulation runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from robot.base.protocol.manifest import Manifest


@dataclass(frozen=True)
class SceneChoice:
    """One scene a simulation can start from.

    `group` and `name` are what a person picks from, e.g. a task and one of its layouts. `config` is what
    the driver is constructed with, and `scene` the first scene, in the driver's own format (JSON-ready).
    """

    group: str
    name: str
    config: dict[str, Any]
    scene: Any


class RobotDriver(ABC):
    """One simulator. The constructor takes the configuration only; `start()` does the heavy work."""

    # The conversation's robot folder, set by the simulation before `start()`: a driver may keep files of its own
    # there, such as videos. Nodes do not read them.
    folder: Path | None = None

    @classmethod
    def scenes(cls) -> list[SceneChoice]:
        """What this simulator can start from; cheap, nothing is started. Empty when it cannot say."""
        return []

    @classmethod
    def config_path(cls) -> Path | None:
        """The config.json of this environment (see `robot.base.config`); None when it has none."""
        return None

    # -- the robot (asked once, after start) ---------------------------------

    @abstractmethod
    def describe(self, body: str) -> str:
        """A paragraph saying what `body` is; its body.md gets it as it is.

        It says nothing of the other bodies, and gives poses in the body's own frame: this is all a node of
        the body reads of the robot.
        """

    @abstractmethod
    def arm_names(self) -> list[str]:
        """The arms nodes can use, e.g. ["left_arm", "right_arm"]."""

    @abstractmethod
    def camera_names(self) -> list[str]:
        """Every camera, e.g. ["cam_head", "cam_left_wrist", "cam_right_wrist"]."""

    @abstractmethod
    def manifest(self) -> Manifest:
        """Who owns which arm and camera: the bodies, written down by hand (see `protocol.manifest`).

        It must own exactly `arm_names()` and `camera_names()`; the simulation refuses to run otherwise.
        """

    # -- one snapshot (all called within one tick, while the world stands still) -----

    @abstractmethod
    def get_joint_positions(self, arm: str) -> list[float]:
        """The arm's measured joint angles, in radians."""

    @abstractmethod
    def get_ee_pose(self, arm: str) -> list[float]:
        """The arm's measured end-effector pose [x, y, z, qw, qx, qy, qz], world frame, metres."""

    @abstractmethod
    def get_gripper(self, arm: str) -> float:
        """The measured gripper opening, from 0 (closed) to 1 (fully open)."""

    @abstractmethod
    def get_images(self) -> dict[str, np.ndarray]:
        """Every camera's current image: {camera: H×W×3 uint8 RGB array}, all taken at this moment."""

    @abstractmethod
    def get_intrinsics(self, camera: str) -> np.ndarray:
        """The camera's 3×3 intrinsic matrix."""

    @abstractmethod
    def get_extrinsics(self, camera: str) -> np.ndarray:
        """The camera's 4×4 camera-to-world matrix, in the world frame of the end-effector poses."""

    def get_extra(self) -> dict[str, Any]:
        """Optional fields for the observation's `extra`, e.g. the task instruction. JSON-ready."""
        return {}

    # -- execution -------------------------------------------------------------

    def pose_parameters(self) -> dict[str, tuple[str, ...]]:
        """Which parameters of which action types are poses, e.g. {"move_ee": ("pose",)}.

        A node gives them in its body's frame; the simulation turns them into the world frame before
        `execute_action` gets them, so a driver always works in the world frame.
        """
        return {}

    @abstractmethod
    def execute_action(self, actions: dict[str, dict[str, Any]]) -> dict[str, str]:
        """Advance the simulation by one tick, each arm doing its current action.

        `actions` maps every arm that has an action to {"id", "action_type", "parameters"}; an arm not in
        it holds still. An id seen for the first time is an action that starts with this tick, and an id
        no longer passed is one the driver can forget. How the arms combine into one simulator step is
        decided here.

        Returns {arm: result} for the actions that ended with this tick. A result is one readable
        sentence, as in 1.0: one starting with "Error:" or "Failed:" fails the action, any other
        completes it. An action type or parameters the driver does not support should end at once with a
        "Failed: ..." result. An exception fails every action passed in.
        """

    # -- scenes (used by the simulation only) ------------------------------------

    @abstractmethod
    def save_scene(self, path: Path) -> None:
        """Write the whole scene of this moment to the file `path`, so that `load_scene` can bring it back exactly.

        Everything that makes the world what it is: every object's pose and velocity, joints, deformables
        and fluids, the last command each arm was given, how far the task has gone. The format is the
        driver's own. It is called after every tick, so it should be quick; a slow one can copy the state
        in memory and write it on a thread of its own, as long as the file is complete when it returns.
        """

    @abstractmethod
    def load_scene(self, path: Path) -> None:
        """Bring back the scene stored in `path` (a file `save_scene` wrote, or a first scene in that format).

        Return once the world is exactly as stored and a snapshot can be taken. When the file is
        missing or unreadable, raise. The actions that were running are abandoned; their ids are not
        passed again.
        """

    # -- lifecycle -----------------------------------------------------------------

    @abstractmethod
    def start(self) -> None:
        """Start the simulator. The simulation loads the scene right after, with `load_scene`."""

    @abstractmethod
    def close(self) -> None:
        """Stop the simulator and release what it holds. Must be safe after a failed start, and twice."""

    def is_finished(self) -> bool:
        """Whether the simulation should end now, e.g. its episode is over. Asked every round."""
        return False
