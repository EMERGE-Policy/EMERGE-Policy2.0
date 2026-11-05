"""Taking a snapshot from a driver: every arm and every camera at the tick, all of this moment.

Every driver call happens here, one after another, while the world stands still. What each call returns is
the driver's contract (`RobotDriver`); it is not checked again here. This only turns the answers into what
the files hold: numpy arrays into plain lists, images into JPEG files.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from robot.base.protocol.fileio import write_bytes
from robot.base.protocol.paths import image_path
from robot.base.protocol.snapshots import ArmState, Camera, Observation, Snapshot, State
from robot.base.simulation.driver import RobotDriver

# High quality: small files, still close enough to the original for perception.
JPEG_QUALITY = 95


def capture(
    driver: RobotDriver, arms: Sequence[str], cameras: Sequence[str], *, tick: int, folder: Path, session_dir: Path
) -> Snapshot:
    """The snapshot of `tick`. Its images are written under obs/<tick>/ as it is taken; the rest is not."""
    state = State(tick, {
        arm: ArmState(
            joint_positions=_floats(driver.get_joint_positions(arm)),
            ee_pose=_floats(driver.get_ee_pose(arm)),
            gripper=float(driver.get_gripper(arm)),
        )
        for arm in arms
    })
    images = driver.get_images()
    recorded: dict[str, Camera] = {}
    for camera in cameras:
        path = image_path(folder, tick, camera)
        write_bytes(path, _jpeg(images[camera]))
        recorded[camera] = Camera(
            image=path.relative_to(session_dir).as_posix(),
            intrinsics=_floats(driver.get_intrinsics(camera)),
            extrinsics=_floats(driver.get_extrinsics(camera)),
        )
    return Snapshot(state, Observation(tick, recorded, dict(driver.get_extra())))


def _floats(value: Any) -> Any:
    """A numpy array (or anything array-like) as nested lists of plain floats, which JSON can hold."""
    return np.asarray(value, dtype=float).tolist()


def _jpeg(image: Any) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.asarray(image)).save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
