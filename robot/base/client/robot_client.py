"""`RobotClient`: what a node uses to operate the robot.

A node holds and releases arms, sends actions, and reads the robot's state and observations. Every method
is one read of the conversation's robot folder, or one change to ACTION.json under its lock, and returns
at once: nothing here waits for the simulation. How long to wait for an action, and what to do meanwhile,
is the node's own rule. Scenes are not a node's business.

In a node:

    robot = RobotClient(ctx.session_dir, run=ctx.run_id, node=ctx.node_id)

An environment's client subclasses it and adds helpers for its own actions (`RoboDojoClient.move_joints`).

The rules it keeps whenever it changes ACTION.json:

    held    an arm another node of this run holds is not this node's to use      ArmHeld
    busy    an arm with an unfinished action of this run takes no new one        ArmBusy
    whole   a send to several arms adds all of its actions or none: a refusal
            raises inside `ActionFile.edit`, which then writes nothing

Within a conversation one run uses the robot at a time. What a run that has ended left behind (its holders,
its unfinished actions) does not stand in the way of the next one.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robot.base.errors import ArmBusy, ArmHeld, RobotError
from robot.base.protocol.actions import Action, ActionFile, Arm, Holder
from robot.base.protocol.fileio import read_json
from robot.base.protocol.lifecycle import new_id
from robot.base.protocol.paths import description_path, observation_path, robot_dir, state_path


class RobotClient:
    """Node `node` of run `run`, using the robot of the conversation in `session_dir` (`ctx.session_dir`)."""

    def __init__(self, session_dir: Path | str, run: str, node: str) -> None:
        for what, value in (("run", run), ("node", node)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"a RobotClient needs a {what} name")
        self.run = run
        self.node = node
        self.session_dir = Path(session_dir)
        self.folder = robot_dir(self.session_dir)

    # -- the robot -------------------------------------------------------------------

    def read_description(self) -> str:
        """What this robot is, in prose (ROBOT.md)."""
        try:
            return description_path(self.folder).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise RobotError(f"no robot description in {self.folder}; is the simulation running?") from None

    # -- arms ------------------------------------------------------------------------

    def hold(self, arm: str) -> None:
        """Hold `arm`: until it is released, the simulation waits for this node whenever the arm has no action.

        Holding an arm this node already holds does nothing. Raises ArmHeld when another node of this run
        holds it; a holder from another run counts as nobody.
        """
        with ActionFile.edit(self.folder) as action_file:
            entry = action_file.arm(arm)
            self._refuse_if_held(arm, entry)
            entry.holder = Holder(self.run, self.node)

    def release(self, arm: str) -> None:
        """Let `arm` go once this node will send it nothing more; the simulation stops waiting for it.

        Releasing an arm nobody holds does nothing. Raises ArmHeld when another node of this run holds it.
        """
        with ActionFile.edit(self.folder) as action_file:
            entry = action_file.arm(arm)
            self._refuse_if_held(arm, entry)
            entry.holder = None

    # -- actions -----------------------------------------------------------------------

    def send_action(self, actions: Mapping[str, tuple[str, Mapping[str, Any]]], step: int) -> dict[str, str]:
        """Send actions, {arm: (action_type, parameters)}, decided on the snapshot of tick `step`.

        Actions sent together start with the same tick. Returns {arm: action id}. Nothing at all is sent
        when one of the arms is held by another node of this run (ArmHeld) or still has an action of this
        run that has not ended (ArmBusy). An unfinished action another run left is replaced.
        """
        ids: dict[str, str] = {}
        with ActionFile.edit(self.folder) as action_file:
            for arm, (action_type, parameters) in actions.items():
                entry = action_file.arm(arm)
                self._refuse_if_held(arm, entry)
                self._refuse_if_busy(arm, entry)
                ids[arm] = new_id()
                entry.actions.append(
                    Action(id=ids[arm], run=self.run, action_type=action_type, parameters=dict(parameters), step=step)
                )
        return ids

    def read_action(self, arm: str) -> dict[str, Any] | None:
        """The arm's current action, with its status and result; None when it has had none."""
        action = ActionFile.read(self.folder).arm(arm).current
        return action.to_json() if action is not None else None

    def read_actions(self, arm: str) -> list[dict[str, Any]]:
        """Every action the arm was sent, oldest first; the last is the current one."""
        return [action.to_json() for action in ActionFile.read(self.folder).arm(arm).actions]

    # -- state and observations ----------------------------------------------------------

    def read_state(self, tick: int | None = None) -> dict[str, Any] | None:
        """The latest state (STATE.json), or the one of `tick`: every arm's joints, end-effector pose and
        gripper. None when there is no such state.

        STATE.json is written last of its tick, so once it shows a tick, `read_observation(state["tick"])`
        is the observation taken with it.
        """
        return read_json(state_path(self.folder, tick))

    def read_observation(self, tick: int | None = None) -> dict[str, Any] | None:
        """The latest observation (OBSERVATION.json), or the one of `tick`: every camera's image and
        matrices. None when there is no such observation. Image paths are relative to `session_dir`."""
        return read_json(observation_path(self.folder, tick))

    # -- the rules ---------------------------------------------------------------------

    def _refuse_if_held(self, name: str, arm: Arm) -> None:
        holder = arm.holder
        if holder is not None and holder.run == self.run and holder.node != self.node:
            raise ArmHeld(f"{name} is held by {holder.node!r}")

    def _refuse_if_busy(self, name: str, arm: Arm) -> None:
        action = arm.current
        if action is not None and action.unfinished and action.run == self.run:
            raise ArmBusy(f"{name} still has action {action.id} ({action.status})")
