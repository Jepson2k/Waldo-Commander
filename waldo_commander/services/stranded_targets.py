"""Program targets a moved object leaves behind: poses written in world
coordinates that sat on the object before it moved, and so stayed put."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy.spatial.transform import Rotation
from waldoctl.shapes import Box, Cylinder, Shape, Sphere, param_names, pose_matrix

from waldo_commander.services.fixture_meshes import FixtureMesh
from waldo_commander.services.scene_source import replace_target, target_binding

# How far off an object's surface a target still counts as on it, in metres.
MARGIN_M = 0.02


@dataclass(frozen=True)
class Move:
    """An object that moved: as it was, and the motion that took it away."""

    name: str
    before: Shape | FixtureMesh
    motion: np.ndarray


@dataclass(frozen=True)
class Stranded:
    """A target left behind on *move*'s object, by its source line."""

    line: int
    target_id: str
    move: Move


def moves(
    before: Iterable[Shape],
    after: Iterable[Shape],
    meshes_before: Iterable[FixtureMesh],
    meshes_after: Iterable[FixtureMesh],
) -> list[Move]:
    """The objects carried to a new pose without being resized."""
    out: list[Move] = []
    shapes_after = {s.name: s for s in after}
    for old in before:
        new = shapes_after.get(old.name)
        if (
            new is None
            or type(new) is not type(old)
            or tuple(new.pose) == tuple(old.pose)
            or any(getattr(new, k) != getattr(old, k) for k in param_names(type(old)))
        ):
            continue
        out.append(Move(old.name, old, _motion(old.pose, new.pose)))
    meshes = {m.name: m for m in meshes_after}
    for old in meshes_before:
        new = meshes.get(old.name)
        if (
            new is None
            or tuple(new.pose) == tuple(old.pose)
            or (new.asset, new.scale) != (old.asset, old.scale)
        ):
            continue
        out.append(Move(old.name, old, _motion(old.pose, new.pose)))
    return out


def _motion(before: Sequence[float], after: Sequence[float]) -> np.ndarray:
    return pose_matrix(after) @ np.linalg.inv(pose_matrix(before))


def _holds(item: Shape | FixtureMesh, point: np.ndarray) -> bool:
    """Whether *point* (world, m) is on or within MARGIN_M of *item*."""
    x, y, z = (np.linalg.inv(pose_matrix(item.pose)) @ [*point, 1.0])[:3]
    m = MARGIN_M
    match item:
        case Box():
            return all(
                abs(v) <= d / 2 + m for v, d in zip((x, y, z), (item.x, item.y, item.z))
            )
        case Sphere():
            return math.hypot(x, y, z) <= item.radius + m
        case Cylinder():
            return math.hypot(x, y) <= item.radius + m and abs(z) <= item.length / 2 + m
        case FixtureMesh():
            low, high = item.bounds
            return all(
                lo * item.scale - m <= v <= hi * item.scale + m
                for v, lo, hi in zip((x, y, z), low, high)
            )
    return False


def _world_pose(values: Sequence[float]) -> np.ndarray:
    """A written pose (mm, degrees, rotations about X then the new Y then Z)
    as a world transform in metres."""
    pose = np.eye(4)
    pose[:3, :3] = Rotation.from_euler("XYZ", values[3:6], degrees=True).as_matrix()
    pose[:3, 3] = np.asarray(values[:3], dtype=float) / 1000
    return pose


def stranded(
    moved: Sequence[Move],
    targets: Iterable[tuple[str, int]],
    source: str,
    joint_count: int,
) -> list[Stranded]:
    """The (id, line) *targets* written as world poses that sat on a moved
    object: a joint target or one stored in a frame does not count."""
    out: list[Stranded] = []
    if not moved:
        return out
    for target_id, line in targets:
        try:
            binding = target_binding(source, line, joint_count)
        except ValueError:
            continue
        if binding.kind != "pose" or binding.storage is not None:
            continue
        point = _world_pose(binding.world_values())[:3, 3]
        move = next((m for m in moved if _holds(m.before, point)), None)
        if move is not None:
            out.append(Stranded(line, target_id, move))
    return out


def carried(source: str, left: Sequence[Stranded], joint_count: int) -> str:
    """*source* with each target in *left* moved as its object moved."""
    # From the bottom up, so a rewrite never shifts a line still to come.
    for item in sorted(left, key=lambda s: s.line, reverse=True):
        binding = target_binding(source, item.line, joint_count)
        pose = item.move.motion @ _world_pose(binding.world_values())
        rotation = Rotation.from_matrix(pose[:3, :3]).as_euler("XYZ", degrees=True)
        source, _ = replace_target(
            source,
            item.line,
            joint_count,
            pose=[*(pose[:3, 3] * 1000).tolist(), *rotation.tolist()],
        )
    return source
