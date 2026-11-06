"""`run_simulation`: run a driver behind a conversation's robot folder, on one clock for every body.

The simulation goes round one cycle:

    snapshot -> nodes decide and send actions -> execute them, tick by tick -> all ended -> snapshot -> ...

**Bodies.** The driver's manifest says which body owns which arm and camera; the simulation checks it against
the driver at start and refuses to run when they differ. Every body has a folder of its own, with its own
action.json, snapshots and description. There is still one clock: the bodies share one world, stepped together.

**Starting an execution.** While the world stands still, the simulation waits until every held arm of every
body has an action (a held arm without one belongs to a node still deciding on the snapshot), then starts them
all with the same tick. Arms nobody holds are not waited for.

**During an execution.** Ticks go on until every action of it has ended. An arm whose action ends first
holds still meanwhile; actions sent now wait for the next execution.

**Ending one.** When its last action ends, the snapshots of that tick are published before the endings are
written, so a node that sees its action end can read the world as it is then.

**History.** action.json keeps each arm's current action only, so a tick reads it in the same time however
long the conversation runs. Every action that ends, however it ends, is added once to its body's
history.jsonl, for debugging; nodes do not read it.

**Keeping bodies apart.** Every round the simulation checks each body's action.json again against the
manifest, in case a file was written without a client: an arm the body does not own is dropped, an action type
it may not send fails. Poses in an action are the body's frame; they are turned into the world's before the
driver sees them. A result is written to the body that asked, with other bodies' arm names taken out.

Between ticks the simulation carries out a front end's command (command.json), and after every tick it
saves the scene over `robot/scene`, from which a simulation started again carries on.

**Writing files.** What only the disk needs (encoding images, JSON, saving the scene) is done by one writer
thread (`writer.Writer`), so the clock does not wait for it. The simulation thread takes the snapshot or the
scene while the world stands still, as copies, and hands them over. A snapshot that closes an execution
carries the endings of its actions with it: the writer writes them to action.json only once the snapshot's
files are in place, and the simulation reads action.json again only after that, so a node that sees its
action end can read the world as it is then.

Within a conversation one run uses the robot at a time. When a run shows up that is not the one served
so far, what the earlier run left (its holders, its unfinished actions) is cleared.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import numpy as np

from robot.base.config import Config
from robot.base.errors import RobotError
from robot.base.protocol import snapshots
from robot.base.protocol.actions import Action, ActionFile, Arm
from robot.base.protocol.commands import RESET, Command
from robot.base.protocol.fileio import write_json, write_text
from robot.base.protocol.frames import pose_to_world
from robot.base.protocol.lifecycle import Request, Status
from robot.base.protocol.paths import (
    body_dir, body_path, description_path, history_path, manifest_path, robot_dir, scene_path,
)
from robot.base.simulation.capture import cut, take
from robot.base.simulation.driver import RobotDriver
from robot.base.simulation.writer import Writer

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
            # Let the writer finish what it was given, then fail what has not ended, the actions and the
            # command, so a node waiting on one sees it end.
            simulation.writer.close()
            with simulation.edit_all() as files:
                for arm, action in _unfinished(files).items():
                    simulation._finish(arm, action, "Failed: simulation stopped.")
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
        # Each arm's unfinished action as last seen, by arm: one that is gone the next time was replaced
        # (by another run's node) and ends in history.jsonl all the same.
        self.seen: dict[str, Action] = {}
        # Writes snapshots and scenes while the clock goes on.
        self.writer = Writer()

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
            # Each body's arms, in the manifest's order; each arm's current action stays. Holders belonged to
            # the process that served them, and nothing left unfinished will be carried out now.
            for name, action_file in files.items():
                previous = action_file.arms
                action_file.arms = {arm: Arm(action=previous[arm].action if arm in previous else None)
                                    for arm in self.manifest.body(name).arms}
            for arm, action in _unfinished(files).items():
                self._finish(arm, action, "Failed: simulation restarted.")
        with Command.edit(self.folder) as command:
            if command.unfinished:
                self._end(command, "Failed: simulation restarted.")
        self._publish_snapshot()
        logger.info("simulation running for %s at tick %d: bodies %s, arms %s, cameras %s",
                    session_dir, self.tick, list(self.manifest.bodies), self.manifest.arms, self.manifest.cameras)

    @contextmanager
    def edit_all(self) -> Iterator[dict[str, ActionFile]]:
        """Every body's action.json under its lock, by body. A node takes one lock only, so none can wait on another."""
        with ExitStack() as stack:
            yield {name: stack.enter_context(ActionFile.edit(folder)) for name, folder in self.folders.items()}

    def run(self) -> None:
        while not self.driver.is_finished():
            # action.json is read again only once the snapshot handed over last, and the endings that follow
            # it, are written. In steps, so Ctrl+C is not held up.
            while not self.writer.quiet.wait(0.1):
                pass
            if self._serve_command():
                continue
            if not self._step():
                time.sleep(self.config.poll_interval)

    def _step(self) -> bool:
        """Advance one tick of the execution under way, or of a new one. Returns whether a tick happened.

        While nothing can run, makes sure the stop has its snapshot.
        """
        with self.edit_all() as files:
            self._notice_replaced(files)
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
            execution = {}
            for arm, action in actions.items():
                # Poses come in the body's frame; the driver works in the world's. Anything that is not a pose
                # stays as sent: the driver refuses it in its own words.
                parameters = dict(action.parameters)
                base = self.manifest.body_of_arm(arm).base_pose
                for key in self.driver.pose_parameters().get(action.action_type, ()):
                    try:
                        pose = np.asarray(parameters.get(key), dtype=float)
                    except (TypeError, ValueError):
                        continue
                    if pose.shape == (7,) and np.isfinite(pose).all() and np.linalg.norm(pose[3:]) > 1e-6:
                        parameters[key] = pose_to_world(base, pose)
                execution[arm] = {"id": action.id, "action_type": action.action_type, "parameters": parameters}
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
        tick = self.tick

        def end() -> None:
            # Each result goes to the body that asked, without the names of the other bodies' arms.
            with self.edit_all() as files:
                for arm, result in results.items():
                    body = self.manifest.body_of_arm(arm).name
                    action = files[body].arm(arm).action
                    if action is None or not action.unfinished or action.id != execution.get(arm, {}).get("id"):
                        continue
                    for other in self.manifest.arms:
                        if other not in self.manifest.body(body).arms:
                            result = result.replace(other, "another arm")
                    self._finish(arm, action, result)
                    logger.info("tick %d: %s action %s %s: %s", tick, arm, action.id, action.status, result)

        if results and set(execution) <= set(results):
            # The last of this execution ended: the snapshots go out before the endings become visible, so a
            # node that sees them can read the world as it is now. The writer does both, in that order.
            self._publish_snapshot(then=end)
        elif results:
            # Some end, others go on: no snapshot now, so the endings are written at once.
            end()
        self._save_scene()
        return True

    def _follow_run(self, files: dict[str, ActionFile]) -> None:
        """Serve the run that shows up in the action.json files; when it is a new one, clear what the earlier run left."""
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
        for arm, action in _unfinished(files).items():
            if action.run != new_run:
                self._finish(arm, action, "Failed: run ended.")
        self.run_id = new_run

    def _notice_replaced(self, files: dict[str, ActionFile]) -> None:
        """End, in history.jsonl, the unfinished actions that are gone from action.json since the last look.

        A node may put its action in the place of an unfinished one another run left; the one it replaced
        is never carried out, and without this it would leave no trace. Then remember what is unfinished now.
        """
        current = {arm: entry.action for action_file in files.values() for arm, entry in action_file.arms.items()}
        for arm, action in list(self.seen.items()):
            now = current.get(arm)
            if now is None or now.id != action.id:
                self._finish(arm, action, "Failed: replaced by another action.")
        self.seen = {arm: action for arm, action in current.items() if action is not None and action.unfinished}

    # -- keeping bodies apart ---------------------------------------------------------

    def _enforce(self, files: dict[str, ActionFile]) -> None:
        """Check each body's action.json against the manifest again, for a file written without a client."""
        for name, action_file in files.items():
            body = self.manifest.body(name)
            for arm in [arm for arm in action_file.arms if arm not in body.arms]:
                logger.warning("%s lists arm %r, which body %r does not own: dropped", name, arm, name)
                del action_file.arms[arm]
            for arm, action in action_file.unfinished(Status.PENDING).items():
                if body.allowed_actions is not None and action.action_type not in body.allowed_actions:
                    logger.warning("body %r sent %r, which it may not: failed", name, action.action_type)
                    self._finish(arm, action, f"Failed: body {name!r} may not send {action.action_type!r}.")

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
            self.writer.flush()
            with self.edit_all() as files:
                for arm, action in _unfinished(files).items():
                    self._finish(arm, action, "Failed: scene changed.")
            self._save_scene()
            result = "Scene reset."
        with Command.edit(self.folder) as command:
            if command.id == command_id:
                self._end(command, result)
        return True

    # -- what every tick, start and ending goes through ---------------------------------

    def _publish_snapshot(self, then: Callable[[], None] | None = None) -> None:
        """Take this tick's snapshot now and hand it to the writer; `then` runs once its files are in place."""
        moment = take(self.driver, self.manifest, self.tick)
        self.published = self.tick
        self.writer.snapshot(lambda: snapshots.publish_all(self.folder, cut(
            moment, self.manifest, folder=self.folder, session_dir=self.session_dir,
            jpeg_quality=self.config.jpeg_quality)), then)

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

    def _finish(self, arm: str, action: Action, result: str) -> None:
        """End `arm`'s `action` with `result`, and add it to its body's history.jsonl: every action that ends
        goes through here, once."""
        self._end(action, result)
        self.seen.pop(arm, None)
        # Only this process adds to history.jsonl, and only adds lines: no lock.
        history = history_path(self.folders[self.manifest.body_of_arm(arm).name])
        with history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"arm": arm, **action.to_json()}, ensure_ascii=False) + "\n")

    def _end(self, request: Request, result: str) -> None:
        started = self.started.pop(request.id, None)
        request.end(result, None if started is None else round((time.monotonic() - started) * 1000))
