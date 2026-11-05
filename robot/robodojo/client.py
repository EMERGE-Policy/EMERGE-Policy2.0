"""`RoboDojoClient`: a RobotClient with one helper per RoboDojo action.

Each helper sends one action to one arm and returns its id at once, like `send_action`; `step` is the tick of the
observation the decision was made on.
"""

from __future__ import annotations

from collections.abc import Sequence

from robot.base.client.robot_client import RobotClient


class RoboDojoClient(RobotClient):
    def move_joints(self, arm: str, joints: Sequence[float], step: int, *, gripper: float | None = None,
                    ticks: int = 25) -> str:
        """Move `arm` in a straight line in joint space to `joints` (6 angles, rad) over `ticks` ticks."""
        parameters = {"joints": [float(x) for x in joints], "ticks": ticks}
        if gripper is not None:
            parameters["gripper"] = float(gripper)
        return self.send_action({arm: ("move_joints", parameters)}, step=step)[arm]

    def move_ee(self, arm: str, pose: Sequence[float], step: int, *, gripper: float | None = None,
                ticks: int = 25) -> str:
        """Move `arm`'s end effector to `pose` ([x, y, z, qw, qx, qy, qz], world frame) over `ticks` ticks."""
        parameters = {"pose": [float(x) for x in pose], "ticks": ticks}
        if gripper is not None:
            parameters["gripper"] = float(gripper)
        return self.send_action({arm: ("move_ee", parameters)}, step=step)[arm]

    def set_gripper(self, arm: str, opening: float, step: int, *, ticks: int = 10) -> str:
        """Open (1) or close (0) `arm`'s gripper, or anything between, over `ticks` ticks."""
        return self.send_action({arm: ("set_gripper", {"gripper": float(opening), "ticks": ticks})}, step=step)[arm]

    def stay(self, arm: str, step: int, *, ticks: int = 1) -> str:
        """Keep `arm` where it was last commanded for `ticks` ticks (an action, not RobotClient.hold)."""
        return self.send_action({arm: ("stay", {"ticks": ticks})}, step=step)[arm]
