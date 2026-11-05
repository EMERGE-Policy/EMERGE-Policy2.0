"""Where each file of a conversation's robot folder lives.

Each conversation has its own robot folder, `sessions/<session>/robot/`, next to its runs and memory; one
simulation serves one conversation. The simulation's own files are at the top. Each body has a folder of its
own, and a node of a body reads and writes only there:

    MANIFEST.json                   who owns which arm and camera                     simulation, at start
    COMMAND.json                    the one command for the simulation               front end and simulation
    TICK.json                       the tick of the latest snapshot, of every body   simulation
    scene                           the whole scene, saved over after every tick     simulation
    launch.json                     which driver serves this conversation            front end
    first_scene                     the scene the conversation began with            front end
    simulation.log                  the simulation's output, as a process of its own front end

    <body>/                         one body's folder (`body_dir`): all a node of that body sees
        BODY.json                   what the body owns: its arms and cameras         simulation, at start
        ROBOT.md                    what the body is, in prose                       simulation, at start
        ACTION.json                 per arm: who holds it, the actions sent to it    its node and simulation
        STATE.json                  the latest state of its arms                     simulation
        OBSERVATION.json            the latest observation of its cameras            simulation
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
    """MANIFEST.json of the robot folder: every body's arms and cameras, as the simulation checked them."""
    return folder / "MANIFEST.json"


def body_path(folder: Path) -> Path:
    """BODY.json of a body's folder: what that body owns, and nothing of the others."""
    return folder / "BODY.json"


def description_path(folder: Path) -> Path:
    return folder / "ROBOT.md"


# -- changed by both sides ----------------------------------------------------------


def action_path(folder: Path) -> Path:
    return folder / "ACTION.json"


def command_path(folder: Path) -> Path:
    """COMMAND.json of the robot folder: it is the simulation's, not a body's."""
    return folder / "COMMAND.json"


# -- snapshots ------------------------------------------------------------------------


def state_path(folder: Path, tick: int | None = None) -> Path:
    """STATE.json, the latest state; or, given `tick`, the state kept for it."""
    return folder / "STATE.json" if tick is None else tick_dir(folder, tick) / "state.json"


def observation_path(folder: Path, tick: int | None = None) -> Path:
    """OBSERVATION.json, the latest observation; or, given `tick`, the observation kept for it."""
    return folder / "OBSERVATION.json" if tick is None else tick_dir(folder, tick) / "observation.json"


def image_path(folder: Path, tick: int, camera: str) -> Path:
    return tick_dir(folder, tick) / f"{camera}.jpg"


def history_dir(folder: Path) -> Path:
    """`obs/`: one folder per tick that was published."""
    return folder / "obs"


def tick_dir(folder: Path, tick: int) -> Path:
    return history_dir(folder) / str(tick)


def tick_path(folder: Path) -> Path:
    """TICK.json of the robot folder: written after every body has its snapshot of that tick."""
    return folder / "TICK.json"


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
