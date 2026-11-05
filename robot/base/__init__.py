"""What every environment builds on: the robot layer, minus any one simulator.

Three sides that never call each other, and the files they share:

    protocol/     the robot folder: where each file is, what it holds, how it is written
    client/       the nodes' side: hold arms, send actions, read state and observations
    simulation/   the simulator's side: a driver, run on one clock behind the robot folder
    frontend/     the front end's side: start a simulation, steer it with commands
    errors        the errors all sides raise

An environment (e.g. robot/robodojo/) subclasses `RobotDriver` to run its simulator and `RobotClient`
to give its nodes helpers for its own actions, then registers both in `frontend/registry`.
"""

from robot.base.client import RobotClient
from robot.base.errors import ArmBusy, ArmHeld, BodyViolation, CommandBusy, ManifestError, RobotError
from robot.base.frontend import (
    Launch,
    SimulationProcess,
    environment_names,
    load_client_class,
    load_driver_class,
    read_command,
    record_launch,
    recorded_launch,
    send_command,
)
from robot.base.simulation import RobotDriver, SceneChoice, run_simulation

__all__ = [
    "ArmBusy",
    "ArmHeld",
    "BodyViolation",
    "CommandBusy",
    "ManifestError",
    "Launch",
    "RobotClient",
    "RobotDriver",
    "RobotError",
    "SceneChoice",
    "SimulationProcess",
    "environment_names",
    "load_client_class",
    "load_driver_class",
    "read_command",
    "record_launch",
    "recorded_launch",
    "run_simulation",
    "send_command",
]
