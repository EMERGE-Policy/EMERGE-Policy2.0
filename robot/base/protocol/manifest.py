"""The manifest: which body owns which arm, and what each body sees, written down once.

A **body** is what one node operates: its arms, the cameras it sees, the `extra` fields it may read, its own
frame for poses. Bodies do not know each other; this file is the only place that says what belongs to whom,
and every side checks against it.

    {
      "bodies": {
        "left":  {"arms": ["left_arm"], "cameras": ["cam_head", "cam_left_wrist"], "extra": ["instruction"],
                  "base_pose": [-0.3, -0.45, 0.765, 0.7071, 0, 0, 0.7071],
                  "allowed_actions": ["move_joints", "move_ee", "set_gripper", "stay"]},
        "right": {...}
      }
    }

    arms             every arm of the body; an arm belongs to exactly one body
    cameras          every camera the body sees; a camera two bodies both see is listed by both
    extra            the fields of the observation's `extra` the body sees; the others are dropped
    base_pose        [x, y, z, qw, qx, qy, qz]: the body's frame in the world. Poses a body reads and sends
                     are in this frame. Left out, the body works in the world frame
    allowed_actions  the action types the body may send. Left out, any the driver supports

The manifest is written by hand, not worked out from a driver: stating who owns what is the point. At start
the simulation checks it against the driver's arms and cameras (`Manifest.check`), and refuses to run when
they differ: every arm and camera of the driver must be in some body, and the manifest must not have more.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robot.base.errors import ManifestError
from robot.base.protocol.fileio import read_json

_BODY_KEYS = {"arms", "cameras", "extra", "base_pose", "allowed_actions"}
_MANIFEST_KEYS = {"bodies"}


@dataclass(frozen=True)
class Body:
    """One body: what it owns and sees, the frame it works in, and what it may do."""

    name: str
    arms: tuple[str, ...]
    cameras: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    base_pose: tuple[float, ...] | None = None
    allowed_actions: tuple[str, ...] | None = None

    @classmethod
    def from_json(cls, name: str, value: Any) -> Body:
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(f"a body needs a name, got {name!r}")
        if not isinstance(value, dict):
            raise ManifestError(f"body {name!r} must be a JSON object, got {value!r}")
        unknown = set(value) - _BODY_KEYS
        if unknown:
            raise ManifestError(f"body {name!r} has unknown keys {sorted(unknown)}; the keys are {sorted(_BODY_KEYS)}")
        arms = _names(value.get("arms"), f"body {name!r} arms")
        if not arms:
            raise ManifestError(f"body {name!r} has no arms")
        allowed = value.get("allowed_actions")
        return cls(
            name=name,
            arms=arms,
            cameras=_names(value.get("cameras", []), f"body {name!r} cameras"),
            extra=_names(value.get("extra", []), f"body {name!r} extra"),
            base_pose=_pose(value.get("base_pose"), f"body {name!r} base_pose"),
            allowed_actions=None if allowed is None else _names(allowed, f"body {name!r} allowed_actions"),
        )

@dataclass(frozen=True)
class Manifest:
    """Every body."""

    bodies: dict[str, Body]

    def __post_init__(self) -> None:
        owners: dict[str, str] = {}
        for body in self.bodies.values():
            for arm in body.arms:
                if arm in owners:
                    raise ManifestError(f"arm {arm!r} belongs to both {owners[arm]!r} and {body.name!r}; it must have one owner")
                owners[arm] = body.name

    # -- who owns what ---------------------------------------------------------------

    def body(self, name: str) -> Body:
        """The body called `name`. Raises ManifestError for a body the manifest does not have."""
        if name not in self.bodies:
            raise ManifestError(f"no body {name!r}; the bodies are {list(self.bodies)}")
        return self.bodies[name]

    def body_of_arm(self, arm: str) -> Body:
        for body in self.bodies.values():
            if arm in body.arms:
                return body
        raise ManifestError(f"no body owns arm {arm!r}")

    @property
    def arms(self) -> list[str]:
        return [arm for body in self.bodies.values() for arm in body.arms]

    @property
    def cameras(self) -> list[str]:
        """Every camera some body sees, once each."""
        return list(dict.fromkeys(camera for body in self.bodies.values() for camera in body.cameras))

    # -- against a driver ------------------------------------------------------------

    def check(self, arms: Iterable[str], cameras: Iterable[str]) -> None:
        """Raise ManifestError unless the manifest has exactly these arms and cameras."""
        problems = []
        for what, listed, driven in (("arm", self.arms, set(arms)), ("camera", self.cameras, set(cameras))):
            for name in sorted(driven - set(listed)):
                problems.append(f"the driver has {what} {name!r}, which no body has")
            for name in sorted(set(listed) - driven):
                problems.append(f"the manifest lists {what} {name!r}, which the driver does not have")
        if problems:
            raise ManifestError("the manifest does not match the driver: " + "; ".join(problems))

    # -- file --------------------------------------------------------------------------

    @classmethod
    def from_json(cls, value: Any) -> Manifest:
        if not isinstance(value, dict):
            raise ManifestError(f"a manifest must be a JSON object, got {type(value).__name__}")
        unknown = set(value) - _MANIFEST_KEYS
        if unknown:
            raise ManifestError(f"unknown manifest keys {sorted(unknown)}; the keys are {sorted(_MANIFEST_KEYS)}")
        bodies = value.get("bodies")
        if not isinstance(bodies, dict) or not bodies:
            raise ManifestError('a manifest needs a non-empty "bodies" object')
        return cls({name: Body.from_json(name, body) for name, body in bodies.items()})

    @classmethod
    def read(cls, path: Path | str) -> Manifest:
        """The manifest in the file `path`."""
        path = Path(path)
        try:
            value = read_json(path)
        except ValueError as e:
            raise ManifestError(f"{path} is not valid JSON: {e}") from None
        if value is None:
            raise ManifestError(f"no manifest at {path}")
        return cls.from_json(value)


def _names(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ManifestError(f"{what} must be a list of names, got {value!r}")
    if len(set(value)) != len(value):
        raise ManifestError(f"{what} lists a name twice: {value!r}")
    return tuple(value)


def _pose(value: Any, what: str) -> tuple[float, ...] | None:
    if value is None:
        return None
    ok = isinstance(value, list) and len(value) == 7 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value
    )
    if not ok:
        raise ManifestError(f"{what} must be [x, y, z, qw, qx, qy, qz], got {value!r}")
    if abs(math.sqrt(sum(item * item for item in value[3:])) - 1.0) > 1e-3:
        raise ManifestError(f"{what} has a quaternion that is not a unit one: {value[3:]!r}")
    return tuple(float(item) for item in value)
