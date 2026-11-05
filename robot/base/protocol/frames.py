"""Poses in a body's frame, and in the world's.

A body works in its own frame, placed in the world by its `base_pose` (see `protocol.manifest`): what it
reads (end-effector poses, camera extrinsics) and what it sends (a `move_ee` pose) are in that frame, so it
never needs to know where its base stands, nor where the others do. A body without a `base_pose` works in
the world frame, and nothing here changes a pose for it.

A pose is [x, y, z, qw, qx, qy, qz]; a matrix is 4x4, as the driver gives them. The simulation turns what
the driver says (world) into the body's frame when it publishes, and what a node sends back into the world
before the driver sees it, with the same two functions on both sides.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def pose_to_body(base_pose: Sequence[float] | None, pose: Sequence[float]) -> list[float]:
    """`pose`, given in the world frame, in the frame of the body whose base is `base_pose`."""
    if base_pose is None:
        return [float(item) for item in pose]
    rotation = _rotation(base_pose[3:])
    position = rotation.T @ (np.asarray(pose[:3], dtype=float) - np.asarray(base_pose[:3], dtype=float))
    orientation = _multiply(_conjugate(base_pose[3:]), pose[3:])
    return [float(item) for item in (*position, *orientation)]


def pose_to_world(base_pose: Sequence[float] | None, pose: Sequence[float]) -> list[float]:
    """`pose`, given in the frame of the body whose base is `base_pose`, in the world frame."""
    if base_pose is None:
        return [float(item) for item in pose]
    rotation = _rotation(base_pose[3:])
    position = rotation @ np.asarray(pose[:3], dtype=float) + np.asarray(base_pose[:3], dtype=float)
    orientation = _multiply(base_pose[3:], pose[3:])
    return [float(item) for item in (*position, *orientation)]


def matrix_to_body(base_pose: Sequence[float] | None, matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    """A 4x4 camera-to-world matrix as camera-to-body: the camera placed in the body's frame."""
    matrix = np.asarray(matrix, dtype=float)
    if base_pose is None:
        return matrix.tolist()
    base = np.eye(4)
    base[:3, :3] = _rotation(base_pose[3:])
    base[:3, 3] = base_pose[:3]
    return (np.linalg.inv(base) @ matrix).tolist()


def _rotation(quaternion: Sequence[float]) -> np.ndarray:
    w, x, y, z = _unit(quaternion)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def _multiply(a: Sequence[float], b: Sequence[float]) -> list[float]:
    """The Hamilton product a * b of two quaternions [w, x, y, z]; the result is a unit one."""
    aw, ax, ay, az = _unit(a)
    bw, bx, by, bz = _unit(b)
    return list(_unit([
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ]))


def _conjugate(quaternion: Sequence[float]) -> list[float]:
    w, x, y, z = quaternion
    return [w, -x, -y, -z]


def _unit(quaternion: Sequence[float]) -> np.ndarray:
    value = np.asarray(quaternion, dtype=float)
    return value / np.linalg.norm(value)
