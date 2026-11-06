"""Taking a snapshot from a driver, and writing it body by body: two halves, on two threads.

`take` makes every driver call, one after another, on the simulation thread while the world stands still,
and keeps the answers as copies that no longer touch the simulator (a `Moment`). What each call returns is
the driver's contract (`RobotDriver`); it is not checked again here.

`cut` runs on the writer thread (`simulation.writer`): it turns a moment into files (numpy arrays into plain
lists, images into JPEG files) and gives each body its own view of them, by the manifest: its arms, the cameras
it sees, poses in its own frame, the `extra` fields it may see. Nothing of another body's goes into a body's
files. It never calls the driver.
"""

from __future__ import annotations

import io
from copy import deepcopy
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Moment:
    """What the driver said at one tick, in the world frame: copies, safe to hand to another thread."""

    tick: int
    arms: dict[str, ArmState]
    images: dict[str, np.ndarray]
    intrinsics: dict[str, Any]
    extrinsics: dict[str, Any]
    extra: dict[str, Any]


def take(driver: RobotDriver, manifest: Manifest, tick: int) -> Moment:
    """Every arm and every camera of `tick`, all of this moment. Driver calls only; nothing is written."""
    arms = {
        arm: ArmState(
            joint_positions=_floats(driver.get_joint_positions(arm)),
            ee_pose=_floats(driver.get_ee_pose(arm)),
            gripper=float(driver.get_gripper(arm)),
        )
        for arm in manifest.arms
    }
    images = driver.get_images()
    return Moment(
        tick=tick,
        arms=arms,
        # Copied: the driver may hand out a buffer the simulator writes into on the next tick.
        images={camera: np.array(images[camera], copy=True) for camera in manifest.cameras},
        intrinsics={camera: _floats(driver.get_intrinsics(camera)) for camera in manifest.cameras},
        extrinsics={camera: _floats(driver.get_extrinsics(camera)) for camera in manifest.cameras},
        extra=deepcopy(dict(driver.get_extra())),
    )


def cut(
    moment: Moment, manifest: Manifest, *, folder: Path, session_dir: Path, jpeg_quality: int = JPEG_QUALITY
) -> dict[str, Snapshot]:
    """Every body's snapshot of the moment, by body. Their images are written under each body's obs/<tick>/
    here; the rest is not. Each image is encoded once, however many bodies see it."""
    jpegs: dict[str, bytes] = {}
    for camera, image in moment.images.items():
        buffer = io.BytesIO()
        Image.fromarray(image).save(buffer, format="JPEG", quality=jpeg_quality)
        jpegs[camera] = buffer.getvalue()
    snapshots: dict[str, Snapshot] = {}
    for body in manifest.bodies.values():
        base = body.base_pose
        state = State(moment.tick, {
            arm: ArmState(
                joint_positions=moment.arms[arm].joint_positions,
                ee_pose=pose_to_body(base, moment.arms[arm].ee_pose),
                gripper=moment.arms[arm].gripper,
            )
            for arm in body.arms
        })
        recorded: dict[str, Camera] = {}
        for camera in body.cameras:
            path = image_path(body_dir(folder, body.name), moment.tick, camera)
            write_bytes(path, jpegs[camera])
            recorded[camera] = Camera(
                image=path.relative_to(session_dir).as_posix(),
                intrinsics=moment.intrinsics[camera],
                extrinsics=matrix_to_body(base, moment.extrinsics[camera]),
            )
        visible = {key: value for key, value in moment.extra.items() if key in body.extra}
        snapshots[body.name] = Snapshot(state, Observation(moment.tick, recorded, visible))
    return snapshots


def _floats(value: Any) -> Any:
    """A numpy array (or anything array-like) as nested lists of plain floats, which JSON can hold."""
    return np.asarray(value, dtype=float).tolist()
