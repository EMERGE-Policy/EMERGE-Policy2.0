"""Taking a snapshot from a driver: every arm and every camera at the tick, all of this moment, then cut
body by body.

Every driver call happens here, one after another, while the world stands still. What each call returns is
the driver's contract (`RobotDriver`); it is not checked again here. This turns the answers into what the
files hold (numpy arrays into plain lists, images into JPEG files) and gives each body its own view of them,
by the manifest: its arms, the cameras it sees, poses in its own frame, the `extra` fields it may see.
Nothing of another body's goes into a body's files.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from robot.base.protocol.fileio import write_bytes
from robot.base.protocol.frames import matrix_to_body, pose_to_body
from robot.base.protocol.manifest import Manifest
from robot.base.protocol.paths import body_dir, image_path
from robot.base.protocol.snapshots import ArmState, Camera, Observation, Snapshot, State
from robot.base.simulation.driver import RobotDriver

# High quality: small files, still close enough to the original for perception.
JPEG_QUALITY = 95


def capture(
    driver: RobotDriver,
    manifest: Manifest,
    *,
    tick: int,
    folder: Path,
    session_dir: Path,
    jpeg_quality: int = JPEG_QUALITY,
) -> dict[str, Snapshot]:
    """The snapshot of `tick` of every body, by body. Their images are written under each body's obs/<tick>/
    as they are taken; the rest is not."""
    arms = {
        arm: (_floats(driver.get_joint_positions(arm)), _floats(driver.get_ee_pose(arm)), float(driver.get_gripper(arm)))
        for arm in manifest.arms
    }
    images = driver.get_images()
    jpegs = {camera: _jpeg(images[camera], jpeg_quality) for camera in manifest.cameras}
    intrinsics = {camera: _floats(driver.get_intrinsics(camera)) for camera in manifest.cameras}
    extrinsics = {camera: _floats(driver.get_extrinsics(camera)) for camera in manifest.cameras}
    extra = dict(driver.get_extra())

    snapshots: dict[str, Snapshot] = {}
    for body in manifest.bodies.values():
        base = body.base_pose
        state = State(tick, {
            arm: ArmState(joint_positions=arms[arm][0], ee_pose=pose_to_body(base, arms[arm][1]), gripper=arms[arm][2])
            for arm in body.arms
        })
        recorded: dict[str, Camera] = {}
        for camera in manifest.visible_cameras(body.name):
            path = image_path(body_dir(folder, body.name), tick, camera)
            write_bytes(path, jpegs[camera])
            recorded[camera] = Camera(
                image=path.relative_to(session_dir).as_posix(),
                intrinsics=intrinsics[camera],
                extrinsics=matrix_to_body(base, extrinsics[camera]),
            )
        visible = {key: value for key, value in extra.items() if key in body.extra}
        snapshots[body.name] = Snapshot(state, Observation(tick, recorded, visible))
    return snapshots


def _floats(value: Any) -> Any:
    """A numpy array (or anything array-like) as nested lists of plain floats, which JSON can hold."""
    return np.asarray(value, dtype=float).tolist()


def _jpeg(image: Any, quality: int) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.asarray(image)).save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()
