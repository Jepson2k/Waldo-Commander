"""Fixture visuals and their proxy membership, owned only by Commander."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import numpy as np
from stl import mesh

MESH_PREFIX = "mesh:"
STORAGE_KEY = "scene_fixture_meshes"
UNITS = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254}


def stl_bounds(data: bytes) -> tuple[tuple[float, ...], tuple[float, ...]]:
    try:
        model = mesh.Mesh.from_file(
            "upload.stl", fh=io.BytesIO(data), calculate_normals=False
        )
        points = np.asarray(model.vectors, dtype=float).reshape(-1, 3)
        if not len(points):
            raise ValueError("The STL has no triangles")
        if not np.isfinite(points).all():
            raise ValueError("The STL contains non-finite coordinates")
        low, high = points.min(axis=0), points.max(axis=0)
        if np.any(high <= low):
            raise ValueError("The STL must have nonzero size on every axis")
        return tuple(map(float, low)), tuple(map(float, high))
    except (ValueError, AssertionError, IndexError, OSError, OverflowError) as error:
        raise ValueError(f"Cannot read this STL: {error}") from error


def mesh_size(bounds, scale: float) -> tuple[float, ...]:
    size = tuple((b - a) * scale for a, b in zip(*bounds))
    if not all(math.isfinite(v) and 0 < v <= 10 for v in size):
        raise ValueError("Choose units giving a nonzero size of at most 10 m per side")
    return size


def asset_path(asset: str) -> Path:
    from .world_files import library_dir

    root = library_dir().resolve()
    path = (root / asset).resolve()
    if Path(asset).is_absolute() or not path.is_relative_to(root / "assets"):
        raise ValueError("Fixture assets must be inside the library's assets folder")
    return path


@dataclass(frozen=True)
class FixtureMesh:
    asset: str
    scale: float
    pose: tuple[float, ...]
    name: str
    proxy_names: tuple[str, ...]
    bounds: tuple[tuple[float, ...], tuple[float, ...]]

    kind: ClassVar[str] = "mesh"
    attachment: ClassVar[None] = None
    collision: ClassVar[bool] = False

    @property
    def key(self) -> str:
        return f"{MESH_PREFIX}{self.name}"

    def to_dict(self) -> dict:
        return dict(
            asset=self.asset,
            scale=self.scale,
            pose=list(map(float, self.pose)),
            name=self.name,
            proxy_names=list(self.proxy_names),
        )


def meshes_from_data(data: object) -> tuple[FixtureMesh, ...]:
    if not isinstance(data, (list, tuple)):
        raise ValueError("meshes must be a list")
    result = []
    names: set[str] = set()
    proxies: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each fixture must be a dictionary")
        asset, name = item["asset"], item["name"]
        scale, pose = float(item["scale"]), tuple(map(float, item["pose"]))
        members = item["proxy_names"]
        if not isinstance(asset, str) or not isinstance(name, str) or not name.strip():
            raise ValueError("Each fixture needs an asset and a nonempty name")
        if name in names:
            raise ValueError("Fixture names must be unique")
        if (
            scale not in UNITS.values()
            or len(pose) != 6
            or not all(map(math.isfinite, pose))
        ):
            raise ValueError("Invalid fixture scale or pose")
        if (
            not isinstance(members, (list, tuple))
            or any(
                not isinstance(n, str) or not n.strip() or n in proxies for n in members
            )
            or len(set(members)) != len(members)
        ):
            raise ValueError("Each proxy must belong to only one fixture")
        bounds = stl_bounds(asset_path(asset).read_bytes())
        mesh_size(bounds, scale)
        result.append(FixtureMesh(asset, scale, pose, name, tuple(members), bounds))
        names.add(name)
        proxies.update(members)
    return tuple(result)
