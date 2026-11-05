"""Where each file of a conversation's robot folder lives.

Each conversation has its own robot folder, `sessions/<session>/robot/`, next to its runs and memory; one
simulation serves one conversation.

    ROBOT.md                    what the robot is, in prose                      simulation, at start
    ACTION.json                 per arm: who holds it, the actions sent to it    nodes and simulation
    COMMAND.json                the one command for the simulation               front end and simulation
    STATE.json                  the latest state of the arms                     simulation
    OBSERVATION.json            the latest observation of the cameras            simulation
    obs/<tick>/state.json       the state of that tick, kept                     simulation
    obs/<tick>/observation.json the observation of that tick, kept               simulation
    obs/<tick>/<camera>.jpg     that tick's images                               simulation
    scene                       the whole scene, saved over after every tick     simulation
    launch.json                 which driver serves this conversation            front end
    first_scene                 the scene the conversation began with            front end
    simulation.log              the simulation's output, as a process of its own front end

Only this module decides where a file lives; everything else asks for it here.
"""

from __future__ import annotations

from pathlib import Path


def robot_dir(session_dir: Path | str) -> Path:
    """The robot folder of the conversation in `session_dir`."""
    return Path(session_dir) / "robot"


# -- written once, at start ---------------------------------------------------------


def description_path(folder: Path) -> Path:
    return folder / "ROBOT.md"


# -- changed by both sides ----------------------------------------------------------


def action_path(folder: Path) -> Path:
    return folder / "ACTION.json"


def command_path(folder: Path) -> Path:
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
