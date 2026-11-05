"""`run_simulation`: run a driver behind a conversation's robot folder, on one clock for every body.

The simulation goes round one cycle:

    snapshot -> nodes decide and send actions -> execute them, tick by tick -> all ended -> snapshot -> ...

**Bodies.** The driver's manifest says which body owns which arm and camera; the simulation checks it against
the driver at start and refuses to run when they differ. Every body has a folder of its own, with its own
ACTION.json, snapshots and description. There is still one clock: the bodies share one world, stepped together.

**Starting an execution.** While the world stands still, the simulation waits until every held arm of every
body has an action (a held arm without one belongs to a node still deciding on the snapshot), then starts them
all with the same tick. Arms nobody holds are not waited for.

**During an execution.** Ticks go on until every action of it has ended. An arm whose action ends first
holds still meanwhile; actions sent now wait for the next execution.

**Ending one.** When its last action ends, the snapshots of that tick are published before the endings are
written, so a node that sees its action end can read the world as it is then.

**Keeping bodies apart.** Every round the simulation checks each body's ACTION.json again against the
manifest, in case a file was written without a client: an arm the body does not own is dropped, an action type
it may not send fails. Poses in an action are the body's frame; they are turned into the world's before the
driver sees them. A result is written to the body that asked, with other bodies' arm names taken out.

Between ticks the simulation carries out a front end's command (COMMAND.json), and after every tick it
saves the scene over `robot/scene`, from which a simulation started again carries on.

Within a conversation one run uses the robot at a time. When a run shows up that is not the one served
so far, what the earlier run left (its holders, its unfinished actions) is cleared.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import numpy as np

from robot.base.config import Config
from robot.base.errors import RobotError
from robot.base.protocol import snapshots
from robot.base.protocol.actions import Action, ActionFile, Arm
from robot.base.protocol.commands import RESET, Command
from robot.base.protocol.fileio import write_json, write_text
from robot.base.protocol.frames import pose_to_world
from robot.base.protocol.lifecycle import Request, Status
from robot.base.protocol.paths import body_dir, body_path, description_path, manifest_path, robot_dir, scene_path
from robot.base.simulation.capture import capture
from robot.base.simulation.driver import RobotDriver

logger = logging.getLogger(__name__)


def run_simulation(
    driver: RobotDriver, session_dir: Path | str, first_scene: Path | str, *, config: Config | None = None
) -> None:
    """Start the simulator, serve the conversation in `session_dir` until it is finished, then close it.

    The world starts from `robot/scene`; when the conversation has none yet, it is first copied from
    `first_scene`, which the reset command also goes back to. Returns once `driver.is_finished()` is
    true; a driver error or a KeyboardInterrupt ends it too, and propagates. Actions and a command that
    have not ended by then are failed. `config` (the defaults when not given) holds the knobs.
    """
    config = config or Config.read()
    # Absolute from here on: a driver may change the working directory when it starts.
    session_dir, first_scene = Path(session_dir).resolve(), Path(first_scene).resolve()
    folder = robot_dir(session_dir)
    simulation: _Simulation | None = None
    try:
        driver.start()
        _take_back_sigint()
        folder.mkdir(parents=True, exist_ok=True)
        if not scene_path(folder).exists():
            shutil.copyfile(first_scene, scene_path(folder))
            logger.info("first scene of %s: copied from %s", session_dir, first_scene)
        driver.load_scene(scene_path(folder))
        simulation = _Simulation(driver, session_dir, first_scene, config)
        simulation.run()
    except BaseException as e:
        if not isinstance(e, KeyboardInterrupt):
            logger.exception("the simulation stopped on an error")
        raise
    finally:
        if simulation is not None:
            # Fail what has not ended, the actions and the command, so a node waiting on one sees it end.
            with simulation.edit_all() as files:
                for action in _unfinished(files).values():
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


def _unfinished(files: dict[str, ActionFile], status: Status | None = None) -> dict[str, Action]:
    """Every body's arms' current actions that have not ended, by arm; given `status`, only those in it."""
    found: dict[str, Action] = {}
    for action_file in files.values():
        found.update(action_file.unfinished(status))
    return found


def _is_pose(value: Any) -> bool:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        return False
    return array.shape == (7,) and bool(np.isfinite(array).all()) and float(np.linalg.norm(array[3:])) > 1e-6


class _Simulation:
    """The clock of one conversation, and what moves it on."""

    def __init__(self, driver: RobotDriver, session_dir: Path, first_scene: Path, config: Config) -> None:
        self.driver = driver
        self.session_dir = session_dir
        self.folder = robot_dir(session_dir)
        self.first_scene = first_scene
        self.config = config
        self.manifest = driver.manifest()
        self.manifest.check(driver.arm_names(), driver.camera_names())
        self.folders = {name: body_dir(self.folder, name) for name in self.manifest.bodies}
        self.tick = snapshots.next_tick(self.folder)
        # The tick of the latest snapshot published: one stop gets one snapshot.
        self.published: int | None = None
        # The run being served; None until one shows up.
        self.run_id: str | None = None
        # When each running action or command started (time.monotonic()), for its duration.
        self.started: dict[str, float] = {}

        # Say who owns what, close what the last start left open, and publish the first snapshots.
        write_json(manifest_path(self.folder), {
            "bodies": {body.name: {"arms": list(body.arms), "cameras": list(body.cameras)}
                       for body in self.manifest.bodies.values()},
        })
        for body in self.manifest.bodies.values():
            folder = self.folders[body.name]
            write_text(description_path(folder), driver.describe(body.name))
            write_json(body_path(folder), {
                "arms": list(body.arms),
                "cameras": list(body.cameras),
                "allowed_actions": None if body.allowed_actions is None else list(body.allowed_actions),
            })
        with self.edit_all() as files:
            # Each body's arms, in the manifest's order; the actions sent so far stay. Holders belonged to the
            # process that served them, and nothing left unfinished will be carried out now.
            for name, action_file in files.items():
                previous = action_file.arms
                action_file.arms = {arm: Arm(actions=previous[arm].actions if arm in previous else [])
                                    for arm in self.manifest.body(name).arms}
            for action in _unfinished(files).values():
                self._end(action, "Failed: simulation restarted.")
        with Command.edit(self.folder) as command:
            if command.unfinished:
                self._end(command, "Failed: simulation restarted.")
        self._publish_snapshot()
        logger.info("simulation running for %s at tick %d: bodies %s, arms %s, cameras %s",
                    session_dir, self.tick, list(self.manifest.bodies), self.manifest.arms, self.manifest.cameras)

    @contextmanager
    def edit_all(self) -> Iterator[dict[str, ActionFile]]:
        """Every body's ACTION.json under its lock, by body. A node takes one lock only, so none can wait on another."""
        with ExitStack() as stack:
            yield {name: stack.enter_context(ActionFile.edit(folder)) for name, folder in self.folders.items()}

    def run(self) -> None:
        while not self.driver.is_finished():
            if self._serve_command():
                continue
            if not self._step():
                time.sleep(self.config.poll_interval)

    def _step(self) -> bool:
        """Advance one tick of the execution under way, or of a new one. Returns whether a tick happened.

        While nothing can run, makes sure the stop has its snapshot.
        """
        with self.edit_all() as files:
            self._follow_run(files)
            self._enforce(files)
            actions = _unfinished(files, Status.RUNNING)
            if not actions:
                # No execution under way: start the pending ones, once every held arm has one. A held arm
                # without an action belongs to a node still deciding on the snapshot.
                pending = _unfinished(files, Status.PENDING)
                deciding = any(arm.holder is not None and name not in pending
                               for action_file in files.values() for name, arm in action_file.arms.items())
                actions = {} if deciding else pending
            for action in actions.values():
                if action.status == Status.PENDING:
                    self._start(action)
            execution = {arm: {"id": action.id, "action_type": action.action_type,
                               "parameters": self._world_parameters(arm, action)}
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
            with self.edit_all() as files:
                for arm, result in results.items():
                    body = self.manifest.body_of_arm(arm).name
                    action = files[body].arm(arm).current
                    if action is None or not action.unfinished:
                        continue
                    self._end(action, self._clean(body, result))
                    logger.info("tick %d: %s action %s %s: %s", self.tick, arm, action.id, action.status, result)
                if not _unfinished(files, Status.RUNNING):
                    # The last of this execution ended: the snapshots go out before the endings become
                    # visible, so a node that sees them can read the world as it is now.
                    self._publish_snapshot()
        self._save_scene()
        return True

    def _follow_run(self, files: dict[str, ActionFile]) -> None:
        """Serve the run that shows up in the ACTION.json files; when it is a new one, clear what the earlier run left."""
        runs = {arm.holder.run for action_file in files.values() for arm in action_file.arms.values()
                if arm.holder is not None}
        runs |= {action.run for action in _unfinished(files).values()}
        newcomers = runs - {self.run_id}
        if not newcomers:
            return
        new_run = min(newcomers)
        if self.run_id is not None:
            logger.info("run %s takes over from run %s", new_run, self.run_id)
        for action_file in files.values():
            for arm in action_file.arms.values():
                if arm.holder is not None and arm.holder.run != new_run:
                    arm.holder = None
        for action in _unfinished(files).values():
            if action.run != new_run:
                self._end(action, "Failed: run ended.")
        self.run_id = new_run

    # -- keeping bodies apart ---------------------------------------------------------

    def _enforce(self, files: dict[str, ActionFile]) -> None:
        """Check each body's ACTION.json against the manifest again, for a file written without a client."""
        for name, action_file in files.items():
            body = self.manifest.body(name)
            for arm in [arm for arm in action_file.arms if arm not in body.arms]:
                logger.warning("%s lists arm %r, which body %r does not own: dropped", name, arm, name)
                del action_file.arms[arm]
            for action in action_file.unfinished(Status.PENDING).values():
                if not body.allows(action.action_type):
                    logger.warning("body %r sent %r, which it may not: failed", name, action.action_type)
                    self._end(action, f"Failed: body {name!r} may not send {action.action_type!r}.")

    def _world_parameters(self, arm: str, action: Action) -> dict[str, Any]:
        """The action's parameters for the driver: the poses in them turned from the body's frame into the world's."""
        parameters = dict(action.parameters)
        base = self.manifest.body_of_arm(arm).base_pose
        for key in self.driver.pose_parameters().get(action.action_type, ()):
            # Anything that is not a pose stays as sent: the driver refuses it in its own words.
            if _is_pose(parameters.get(key)):
                parameters[key] = pose_to_world(base, parameters[key])
        return parameters

    def _clean(self, body: str, result: str) -> str:
        """`result` without the names of arms that are not `body`'s: it goes into the body's own file."""
        for arm in self.manifest.arms:
            if self.manifest.body_of_arm(arm).name != body:
                result = result.replace(arm, "another arm")
        return result

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
            with self.edit_all() as files:
                for action in _unfinished(files).values():
                    self._end(action, "Failed: scene changed.")
            self._save_scene()
            result = "Scene reset."
        with Command.edit(self.folder) as command:
            if command.id == command_id:
                self._end(command, result)
        return True

    # -- what every tick, start and ending goes through ---------------------------------

    def _publish_snapshot(self) -> None:
        bodies = capture(self.driver, self.manifest, tick=self.tick, folder=self.folder,
                         session_dir=self.session_dir, jpeg_quality=self.config.jpeg_quality)
        snapshots.publish_all(self.folder, bodies)
        self.published = self.tick
        logger.debug("snapshot of tick %d", self.tick)

    def _save_scene(self) -> None:
        """Save the scene over robot/scene, written aside first so the file is always a whole scene."""
        if not self.config.save_scene:
            return
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
