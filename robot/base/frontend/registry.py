"""Environments by name: each one's `RobotDriver` and the `RobotClient` its nodes use.

Each environment lives in its own folder next to base/ (e.g. robot/robodojo/) and registers here. A
driver that is not registered can still be named as "module:Class" on the command line.
"""

from __future__ import annotations

import importlib
from typing import TypeVar

from robot.base.client.robot_client import RobotClient
from robot.base.errors import RobotError
from robot.base.simulation.driver import RobotDriver

# name -> {"driver": "module:Class", "client": "module:Class"}, e.g.
# "robodojo": {"driver": "robot.robodojo.driver:RoboDojoDriver", "client": "robot.robodojo.client:RoboDojoClient"}
ENVIRONMENTS: dict[str, dict[str, str]] = {
    "robodojo": {"driver": "robot.robodojo.driver:RoboDojoDriver", "client": "robot.robodojo.client:RoboDojoClient"},
}

_Base = TypeVar("_Base", RobotDriver, RobotClient)


def environment_names() -> list[str]:
    """The registered environments, by name."""
    return sorted(ENVIRONMENTS)


def load_driver_class(name: str) -> type[RobotDriver]:
    """The driver of environment `name`, or the class `name` points at as "module:Class"."""
    return _load(name, "driver", RobotDriver)


def load_client_class(name: str) -> type[RobotClient]:
    """The client class nodes use with environment `name`."""
    return _load(name, "client", RobotClient)


def _load(name: str, kind: str, base: type[_Base]) -> type[_Base]:
    target = ENVIRONMENTS[name][kind] if name in ENVIRONMENTS else name
    module_name, _, class_name = target.partition(":")
    if not module_name or not class_name:
        registered = ", ".join(sorted(ENVIRONMENTS)) or "none yet"
        raise RobotError(f"unknown environment {name!r}; registered: {registered}; or give a {kind} as module:Class")
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise RobotError(f"cannot import {kind} module {module_name!r}: {e}") from None
    cls = getattr(module, class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, base)):
        raise RobotError(f"{target} is not a {base.__name__} subclass")
    return cls
