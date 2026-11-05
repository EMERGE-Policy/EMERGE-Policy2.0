"""Run the simulation of one conversation.

    python -m robot --session <folder>
        start the simulation recorded for the conversation, carrying on from its saved scene
    python -m robot --session <folder> --driver <name or module:Class> --scene <first scene> [--config driver.json]
        record a new simulation for it first, starting over from that scene

--session is the conversation folder, sessions/<session>/ in the workspace; the robot files go in its
robot/ folder, the record in robot/launch.json. --scene is a file in the driver's scene format; the config
file holds one JSON object, the driver's constructor arguments. Ctrl+C stops the simulation; unfinished
actions are failed and the simulator is closed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from robot.base.errors import RobotError
from robot.base.frontend.launch import Launch, record_launch, recorded_launch
from robot.base.frontend.registry import load_driver_class
from robot.base.protocol.paths import first_scene_path, robot_dir
from robot.base.simulation.loop import run_simulation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m robot", description="Run the simulation of one conversation.")
    parser.add_argument("--session", type=Path, required=True, help="the conversation folder, sessions/<session>/")
    parser.add_argument("--driver", help="record a new simulation: a registered environment name, or module:Class")
    parser.add_argument("--scene", type=Path, help="with --driver: the scene to start from")
    parser.add_argument("--config", type=Path, help="with --driver: JSON object passed to the driver as keyword arguments")
    parser.add_argument(
        "--poll-interval", type=float, default=0.01, help="seconds between looks at the files while nothing moves"
    )
    args = parser.parse_args(argv)
    if (args.driver is None) != (args.scene is None) or (args.config is not None and args.driver is None):
        parser.error("--driver and --scene go together (with --config optionally), or none of them")
    _log_to_stderr()
    try:
        if args.driver is not None:
            load_driver_class(args.driver)  # a name that is no driver is not recorded
            record_launch(args.session, Launch(args.driver, _read_config(args.config)), _read_scene(args.scene))
        launch = recorded_launch(args.session)
        if launch is None:
            raise RobotError(f"no simulation is recorded for {args.session}; give --driver and --scene")
        try:
            driver = load_driver_class(launch.driver)(**launch.config)
        except TypeError as e:
            raise RobotError(f"cannot construct the {launch.driver} driver with {launch.config}: {e}") from None
        run_simulation(driver, args.session, first_scene_path(robot_dir(args.session)), poll_interval=args.poll_interval)
    except KeyboardInterrupt:
        return 130
    except RobotError as e:
        print(f"robot: {e}", file=sys.stderr)
        return 1
    return 0


def _log_to_stderr() -> None:
    """The robot layer's own log lines, on stderr. On the "robot" logger rather than the root one, which a
    simulator may take over when it starts (Isaac Sim does, and drops INFO lines)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("robot")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _read_scene(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise RobotError(f"no scene file {path}") from None


def _read_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RobotError(f"no config file {path}") from None
    except json.JSONDecodeError as e:
        raise RobotError(f"invalid JSON in {path}: line {e.lineno} column {e.colno}: {e.msg}") from None
    if not isinstance(config, dict):
        raise RobotError(f"{path} must hold one JSON object")
    return config


if __name__ == "__main__":
    sys.exit(main())
