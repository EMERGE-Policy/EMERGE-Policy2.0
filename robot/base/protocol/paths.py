"""Where each file of a conversation's robot folder lives.

Each conversation has its own robot folder, `sessions/<session>/robot/`, next to its runs and memory; one
simulation serves one conversation. The simulation's own files are at the top. Each body has a folder of its
own, and a node of a body reads and writes only there:

    manifest.json                   who owns which arm and camera                     simulation, at start
    command.json                    the one command for the simulation               front end and simulation
    tick.json                       the tick of the latest snapshot, of every body   simulation
    scene                           the whole scene, saved over after every tick     simulation
    launch.json                     which driver serves this conversation            front end
    first_scene                     the scene the conversation began with            front end
    simulation.log                  the simulation's output, as a process of its own front end
    recorder/                       the driver's videos, when it records             driver

    <body>/                         one body's folder (`body_dir`): all a node of that body sees
        body.json                   what the body owns: its arms and cameras         simulation, at start
        body.md                     what the body is, in prose                       simulation, at start
        action.json                 per arm: who holds it, its current action        its node and simulation
        history.jsonl               every action that ended, one per line; debugging simulation
        state.json                  the latest state of its arms                     simulation
        observation.json            the latest observation of its cameras            simulation
        obs/<tick>/state.json       the state of that tick, kept                     simulation
        obs/<tick>/observation.json the observation of that tick, kept               simulation
        obs/<tick>/<camera>.jpg     that tick's images                               simulation

A function below that takes a `folder` for one of those per-body files takes the body's folder, not the
robot folder. Only this module decides where a file lives; everything else asks for it here.
"""

from __future__ import annotations

from pathlib import Path


def robot_dir(session_dir: Path | str) -> Path:
    """The robot folder of the conversation in `session_dir`."""
    return Path(session_dir) / "robot"


def body_dir(folder: Path, body: str) -> Path:
    """The folder of `body` in the robot folder `folder`."""
    return folder / body


# -- written once, at start ---------------------------------------------------------


def manifest_path(folder: Path) -> Path:
    """manifest.json of the robot folder: every body's arms and cameras, as the simulation checked them."""
    return folder / "manifest.json"


def body_path(folder: Path) -> Path:
    """body.json of a body's folder: what that body owns, and nothing of the others."""
    return folder / "body.json"


def description_path(folder: Path) -> Path:
    return folder / "body.md"


# -- changed by both sides ----------------------------------------------------------


def action_path(folder: Path) -> Path:
    return folder / "action.json"


def history_path(folder: Path) -> Path:
    """history.jsonl of a body's folder: every action of the body that ended, for whoever debugs; nodes do not read it."""
    return folder / "history.jsonl"


def command_path(folder: Path) -> Path:
    """command.json of the robot folder: it is the simulation's, not a body's."""
    return folder / "command.json"


# -- snapshots ------------------------------------------------------------------------


def state_path(folder: Path, tick: int | None = None) -> Path:
    """state.json, the latest state; or, given `tick`, the state kept for it."""
    return folder / "state.json" if tick is None else tick_dir(folder, tick) / "state.json"


def observation_path(folder: Path, tick: int | None = None) -> Path:
    """observation.json, the latest observation; or, given `tick`, the observation kept for it."""
    return folder / "observation.json" if tick is None else tick_dir(folder, tick) / "observation.json"


def image_path(folder: Path, tick: int, camera: str) -> Path:
    return tick_dir(folder, tick) / f"{camera}.jpg"


def history_dir(folder: Path) -> Path:
    """`obs/`: one folder per tick that was published."""
    return folder / "obs"


def tick_dir(folder: Path, tick: int) -> Path:
    return history_dir(folder) / str(tick)


def tick_path(folder: Path) -> Path:
    """tick.json of the robot folder: written after every body has its snapshot of that tick."""
    return folder / "tick.json"


# -- the simulation's own -------------------------------------------------------------


def scene_path(folder: Path) -> Path:
    """The whole scene, in the driver's own format; the simulation starts from it."""
    return folder / "scene"


# -- the front end's -------------------------------------------------------------------


def launch_path(folder: Path) -> Path:
    return folder / "launch.json"


def first_scene_path(folder: Path) -> Path:
    """The scene the conversation began with; the reset command goes back to it."""
    return folder / "first_scene"


def log_path(folder: Path) -> Path:
    return folder / "simulation.log"


def recorder_dir(folder: Path) -> Path:
    """Where a driver keeps its videos (`RobotDriver.folder` is the robot folder); nodes do not read them."""
    return folder / "recorder"
