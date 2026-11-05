"""`run_simulation`: run a driver behind a conversation's robot folder, on one clock for every arm.

The simulation goes round one cycle:

    snapshot -> nodes decide and send actions -> execute them, tick by tick -> all ended -> snapshot -> ...

**Starting an execution.** While the world stands still, the simulation waits until every held arm has an
action (a held arm without one belongs to a node still deciding on the snapshot), then starts them all
with the same tick. Arms nobody holds are not waited for.

**During an execution.** Ticks go on until every action of it has ended. An arm whose action ends first
holds still meanwhile; actions sent now wait for the next execution.

**Ending one.** When its last action ends, the snapshot of that tick is published before the ending is
written, so a node that sees its action end can read the world as it is then.

Between ticks the simulation carries out a front end's command (COMMAND.json), and after every tick it
saves the scene over `robot/scene`, from which a simulation started again carries on.

Within a conversation one run uses the robot at a time. When a run shows up that is not the one served
so far, what the earlier run left (its holders, its unfinished actions) is cleared.
"""

from __future__ import annotations

import logging
import os
import signal
import shutil
import time
from pathlib import Path

from robot.base.errors import RobotError
from robot.base.protocol import snapshots
from robot.base.protocol.actions import ActionFile, Arm
from robot.base.protocol.commands import RESET, Command
from robot.base.protocol.fileio import write_text
from robot.base.protocol.lifecycle import Request, Status
from robot.base.protocol.paths import description_path, robot_dir, scene_path
from robot.base.simulation.capture import capture
from robot.base.simulation.driver import RobotDriver

logger = logging.getLogger(__name__)


def run_simulation(
    driver: RobotDriver, session_dir: Path | str, first_scene: Path | str, *, poll_interval: float = 0.01
) -> None:
    """Start the simulator, serve the conversation in `session_dir` until it is finished, then close it.

    The world starts from `robot/scene`; when the conversation has none yet, it is first copied from
    `first_scene`, which the reset command also goes back to. Returns once `driver.is_finished()` is
    true; a driver error or a KeyboardInterrupt ends it too, and propagates. Actions and a command that
    have not ended by then are failed. While nothing can move, the files are looked at every
    `poll_interval` seconds.
    """
    # Absolute from here on: a driver may change the working directory when it starts.
    session_dir, first_scene = Path(session_dir).resolve(), Path(first_scene).resolve()
    folder = robot_dir(session_dir)
    driver.start()
    _take_back_sigint()
    folder.mkdir(parents=True, exist_ok=True)
    if not scene_path(folder).exists():
        shutil.copyfile(first_scene, scene_path(folder))
        logger.info("first scene of %s: copied from %s", session_dir, first_scene)
    driver.load_scene(scene_path(folder))
    simulation = _Simulation(driver, session_dir, first_scene, poll_interval)
    try:
        simulation.run()
    except BaseException as e:
        if not isinstance(e, KeyboardInterrupt):
            logger.exception("the simulation stopped on an error")
        raise
    finally:
        # Fail what has not ended, the actions and the command, so a node waiting on one sees it end.
        with ActionFile.edit(folder) as action_file:
            for action in action_file.unfinished().values():
                simulation._end(action, "Failed: simulation stopped.")
        with Command.edit(folder) as command:
            if command.unfinished:
                simulation._end(command, "Failed: simulation stopped.")
        # Logged before closing: closing may end the whole process (Isaac Sim's does).
        logger.info("closing the simulator")
        driver.close()
        logger.info("simulation closed")


def _take_back_sigint() -> None:
    """Give Ctrl+C back to Python, after a simulator has taken it.

    Isaac Sim installs its own SIGINT handler, which shuts its app down and ends the process from where the
    signal lands: no KeyboardInterrupt is raised, so `run_simulation`'s `finally` never runs and the actions
    that were running are left in the file as if they still were. Restoring the handler Python installed
    makes Ctrl+C a KeyboardInterrupt again, which the `finally` cleans up after.
    """
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        signal.signal(signal.SIGINT, signal.default_int_handler)


class _Simulation:
    """The clock of one conversation, and what moves it on."""

    def __init__(self, driver: RobotDriver, session_dir: Path, first_scene: Path, poll_interval: float) -> None:
        self.driver = driver
        self.session_dir = session_dir
        self.folder = robot_dir(session_dir)
        self.first_scene = first_scene
        self.poll_interval = poll_interval
        self.arms = list(driver.arm_names())
        self.cameras = list(driver.camera_names())
        self.tick = snapshots.next_tick(self.folder)
        # The tick of the latest snapshot published: one stop gets one snapshot.
        self.published: int | None = None
        # The run being served; None until one shows up.
        self.run_id: str | None = None
        # When each running action or command started (time.monotonic()), for its duration.
        self.started: dict[str, float] = {}

        # Describe the robot, close what the last start left open, and publish the first snapshot.
        write_text(description_path(self.folder), driver.describe())
        with ActionFile.edit(self.folder) as action_file:
            # The driver's arms, in its order; the actions sent so far stay. Holders belonged to the
            # process that served them, and nothing left unfinished will be carried out now.
            previous = action_file.arms
            action_file.arms = {name: Arm(actions=previous[name].actions if name in previous else [])
                                for name in self.arms}
            for action in action_file.unfinished().values():
                self._end(action, "Failed: simulation restarted.")
        with Command.edit(self.folder) as command:
            if command.unfinished:
                self._end(command, "Failed: simulation restarted.")
        self._publish_snapshot()
        logger.info("simulation running for %s at tick %d: arms %s, cameras %s",
                    session_dir, self.tick, self.arms, self.cameras)

    def run(self) -> None:
        while not self.driver.is_finished():
            if self._serve_command():
                continue
            if not self._step():
                time.sleep(self.poll_interval)

    def _step(self) -> bool:
        """Advance one tick of the execution under way, or of a new one. Returns whether a tick happened.

        While nothing can run, makes sure the stop has its snapshot.
        """
        with ActionFile.edit(self.folder) as action_file:
            self._follow_run(action_file)
            actions = action_file.unfinished(Status.RUNNING)
            if not actions:
                # No execution under way: start the pending ones, once every held arm has one. A held arm
                # without an action belongs to a node still deciding on the snapshot.
                pending = action_file.unfinished(Status.PENDING)
                deciding = any(arm.holder is not None and name not in pending
                               for name, arm in action_file.arms.items())
                actions = {} if deciding else pending
            for action in actions.values():
                if action.status == Status.PENDING:
                    self._start(action)
            execution = {arm: {"id": action.id, "action_type": action.action_type,
                               "parameters": action.parameters}
                         for arm, action in actions.items()}
        if not execution:
            if self.published != self.tick:
                self._publish_snapshot()
            return False

        try:
            results = self.driver.execute_action(execution) or {}
        except Exception as e:
            logger.exception("execute_action failed at tick %d", self.tick)
            results = {arm: f"Error: {type(e).__name__}: {e}" for arm in execution}
        self.tick += 1
        if results:
            with ActionFile.edit(self.folder) as action_file:
                for arm, result in results.items():
                    action = action_file.arm(arm).current
                    if not action.unfinished:
                        continue
                    self._end(action, result)
                    logger.info("tick %d: %s action %s %s: %s", self.tick, arm, action.id, action.status, result)
                if not action_file.unfinished(Status.RUNNING):
                    # The last of this execution ended: the snapshot goes out before the endings become
                    # visible, so a node that sees them can read the world as it is now.
                    self._publish_snapshot()
        self._save_scene()
        return True

    def _follow_run(self, action_file: ActionFile) -> None:
        """Serve the run that shows up in ACTION.json; when it is a new one, clear what the earlier run left."""
        runs = {arm.holder.run for arm in action_file.arms.values() if arm.holder is not None}
        runs |= {action.run for action in action_file.unfinished().values()}
        newcomers = runs - {self.run_id}
        if not newcomers:
            return
        new_run = min(newcomers)
        if self.run_id is not None:
            logger.info("run %s takes over from run %s", new_run, self.run_id)
        for arm in action_file.arms.values():
            if arm.holder is not None and arm.holder.run != new_run:
                arm.holder = None
        for action in action_file.unfinished().values():
            if action.run != new_run:
                self._end(action, "Failed: run ended.")
        self.run_id = new_run

    # -- commands ---------------------------------------------------------------------

    def _serve_command(self) -> bool:
        """Carry out the pending command, if there is one. Returns whether one was carried out."""
        with Command.edit(self.folder) as command:
            if command.status != Status.PENDING:
                return False
            self._start(command)
            command_id, op = command.id, command.op
        logger.info("command %s: %s", command_id, op)
        try:
            if op != RESET:
                raise RobotError(f"unknown command {op!r}")
            self.driver.load_scene(self.first_scene)
        except Exception as e:
            logger.warning("command %s failed: %s: %s", command_id, type(e).__name__, e)
            result = f"Error: {type(e).__name__}: {e}"
        else:
            # A changed scene is a new moment: publish it, then end the actions meant for the old one.
            self.tick += 1
            self._publish_snapshot()
            with ActionFile.edit(self.folder) as action_file:
                for action in action_file.unfinished().values():
                    self._end(action, "Failed: scene changed.")
            self._save_scene()
            result = "Scene reset."
        with Command.edit(self.folder) as command:
            if command.id == command_id:
                self._end(command, result)
        return True

    # -- stopping ---------------------------------------------------------------------

    # -- what every tick, start and ending goes through ---------------------------------

    def _publish_snapshot(self) -> None:
        snapshot = capture(self.driver, self.arms, self.cameras,
                           tick=self.tick, folder=self.folder, session_dir=self.session_dir)
        snapshots.publish(self.folder, snapshot)
        self.published = self.tick
        logger.debug("snapshot of tick %d", self.tick)

    def _save_scene(self) -> None:
        """Save the scene over robot/scene, written aside first so the file is always a whole scene."""
        target = scene_path(self.folder)
        temporary = target.with_name(f".{target.name}.saving")
        try:
            self.driver.save_scene(temporary)
            os.replace(temporary, target)
        except Exception:
            logger.exception("saving the scene at tick %d failed; robot/scene keeps the previous one", self.tick)
            temporary.unlink(missing_ok=True)

    def _start(self, request: Request) -> None:
        request.start()
        self.started[request.id] = time.monotonic()

    def _end(self, request: Request, result: str) -> None:
        started = self.started.pop(request.id, None)
        request.end(result, None if started is None else round((time.monotonic() - started) * 1000))
