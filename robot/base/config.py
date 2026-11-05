"""config.json: the knobs of the robot layer, for tuning and debugging. Each environment has its own, next to its
driver (robot/robodojo/config.json), and its driver says where (`RobotDriver.config_path`).

    {"poll_interval": 0.01, "jpeg_quality": 95, "log_level": "INFO", "save_scene": true,
     "driver": {"task": "stack_bowls", "device_id": 0}}

    poll_interval   seconds between looks at the files while nothing moves
    jpeg_quality    quality of the images kept under each body's obs/, 1 to 100
    log_level       how much the robot layer logs: DEBUG, INFO, WARNING or ERROR
    save_scene      save the scene after every tick; off, a simulation started again carries on from the
                    last scene saved, not from where it stopped
    driver          defaults for the driver's constructor arguments; what a conversation's launch.json gives
                    (the task, say) takes the place of the same key here

Every key is optional and has the value shown (`driver` is empty). Who owns which arm is not here: that is the
manifest. A key this file does not know is refused, so a typo is never taken for the default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from robot.base.errors import RobotError
from robot.base.protocol.fileio import read_json

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class Config:
    poll_interval: float = 0.01
    jpeg_quality: int = 95
    log_level: str = "INFO"
    save_scene: bool = True
    driver: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        interval = self.poll_interval
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
            raise RobotError(f"poll_interval must be a number above 0, got {interval!r}")
        quality = self.jpeg_quality
        if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
            raise RobotError(f"jpeg_quality must be a whole number from 1 to 100, got {quality!r}")
        if self.log_level not in LOG_LEVELS:
            raise RobotError(f"log_level must be one of {list(LOG_LEVELS)}, got {self.log_level!r}")
        if not isinstance(self.save_scene, bool):
            raise RobotError(f"save_scene must be true or false, got {self.save_scene!r}")
        if not isinstance(self.driver, dict):
            raise RobotError(f"driver must be a JSON object, got {self.driver!r}")

    @classmethod
    def from_json(cls, value: Any) -> Config:
        if not isinstance(value, dict):
            raise RobotError(f"the config must be a JSON object, got {type(value).__name__}")
        known = {item.name for item in fields(cls)}
        unknown = set(value) - known
        if unknown:
            raise RobotError(f"unknown config keys {sorted(unknown)}; the keys are {sorted(known)}")
        return cls(**value)

    @classmethod
    def read(cls, path: Path | str | None = None) -> Config:
        """The config in the file `path`; the defaults when no path is given."""
        if path is None:
            return cls()
        path = Path(path)
        try:
            value = read_json(path)
        except ValueError as e:
            raise RobotError(f"{path} is not valid JSON: {e}") from None
        if value is None:
            raise RobotError(f"no config file {path}")
        return cls.from_json(value)
