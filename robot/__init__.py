"""The robot layer: simulators served to EMERGE nodes through the files in `sessions/<session>/robot/`.

    base/        what every environment builds on: the robot folder (protocol/), the nodes' side
                 (client/), the simulator's side (simulation/) and the front end's (frontend/)
    <env>/       one folder per environment (e.g. robodojo/): its driver and client, built on base

The beat, the file formats and the driver contract are described in docs/robot-interface.md.
"""

from robot.base import (
    ArmBusy,
    ArmHeld,
    BodyViolation,
    CommandBusy,
    ManifestError,
    Launch,
    RobotClient,
    RobotDriver,
    RobotError,
    SceneChoice,
    SimulationProcess,
    environment_names,
    load_client_class,
    load_driver_class,
    read_command,
    record_launch,
    recorded_launch,
    run_simulation,
    send_command,
)

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
