"""The front end's side of the robot: what a TUI (or a script) uses to set up and steer a simulation.

    registry       environments by name, and their driver and client classes
    launch         how a conversation's simulation is started, and running it as a process of its own
    command_port   telling a running simulation what to do (now only reset)
"""

from robot.base.frontend.command_port import read_command, send_command
from robot.base.frontend.launch import Launch, SimulationProcess, record_launch, recorded_launch
from robot.base.frontend.registry import environment_names, load_client_class, load_driver_class

__all__ = [
    "Launch",
    "SimulationProcess",
    "environment_names",
    "load_client_class",
    "load_driver_class",
    "read_command",
    "record_launch",
    "recorded_launch",
    "send_command",
]
