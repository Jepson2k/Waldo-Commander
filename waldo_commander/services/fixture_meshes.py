"""Fixture visuals and their proxy membership, owned only by Commander."""

from __future__ import annotations

import io
import math
import re
import logging
import json
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import numpy as np
from stl import mesh

MESH_PREFIX = "mesh:"
STORAGE_KEY = "scene_fixture_meshes"
MAX_STL_BYTES = 10 * 1024 * 1024
MAX_STL_TRIANGLES = 100_000
UNITS = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254}
logger = logging.getLogger(__name__)
_assets_swept = False


def sweep_unused_assets() -> None:
    """Once per startup, collect all references before removing orphaned STLs."""
    from nicegui import app
    from nicegui.persistence.file_persistent_dict import FilePersistentDict

    from .world_files import entry_asset_references, library_dir, list_entries

    global _assets_swept
    if _assets_swept:
        return
    _assets_swept = True
    try:
        root = library_dir()
        if not root.exists():
            return
        storage = app.storage.general
        manifests = [storage.get(STORAGE_KEY, [])]
        if isinstance(storage, FilePersistentDict) and storage.filepath.exists():
            saved = json.loads(storage.filepath.read_text(encoding=storage.encoding))
            if not isinstance(saved, dict):
                raise ValueError("Saved scene storage must be a dictionary")
            manifests.append(saved.get(STORAGE_KEY, []))
        referenced = set().union(*(asset_references(data) for data in manifests))
        for name in list_entries():
            referenced.update(entry_asset_references(name))
        assets = root / "assets"
        if assets.is_symlink():
            raise ValueError("The assets directory is a symlink")
        candidates = (
            [
                path
                for path in assets.iterdir()
                if path.suffix.lower() == ".stl"
                and not path.is_symlink()
                and path.is_file()
                and path.resolve() not in referenced
            ]
            if assets.exists()
            else []
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        logger.warning("Skipping unused STL sweep: %s", error)
        return
    for path in candidates:
        try:
            path.unlink()
            logger.info("Deleted unused STL asset %s", path)
        except OSError as error:
            logger.warning("Cannot delete unused STL asset %s: %s", path, error)


def stl_bounds(data: bytes) -> tuple[tuple[float, ...], tuple[float, ...]]:
    try:
        if len(data) > MAX_STL_BYTES:
            raise ValueError("STL files must be at most 10 MiB")
        binary_count = int.from_bytes(data[80:84], "little")
        binary = len(data) >= 84 and len(data) == 84 + 50 * binary_count
        count = (
            binary_count
            if binary
            else len(re.findall(rb"(?im)^\s*facet\s+normal\b", data))
        )
        if count > MAX_STL_TRIANGLES:
            raise ValueError("STL files must have at most 100,000 triangles")
        low, high = np.full(3, np.inf), np.full(3, -np.inf)
        triangles = 0
        for model in mesh.Mesh.from_multi_file(
            "upload.stl", fh=io.BytesIO(data), calculate_normals=False
        ):
            triangles += len(model.vectors)
            if triangles > MAX_STL_TRIANGLES:
                raise ValueError("STL files must have at most 100,000 triangles")
            points = model.vectors.reshape(-1, 3)
            if not len(points):
                continue
            if not np.isfinite(points).all():
                raise ValueError("The STL contains non-finite coordinates")
            low = np.minimum(low, points.min(axis=0))
            high = np.maximum(high, points.max(axis=0))
        if not triangles:
            raise ValueError("The STL has no triangles")
        if np.any(high <= low):
            raise ValueError("The STL must have nonzero size on every axis")
        return tuple(map(float, low)), tuple(map(float, high))
    except (ValueError, AssertionError, IndexError, OSError, OverflowError) as error:
        raise ValueError(f"Cannot read this STL: {error}") from error


@lru_cache(maxsize=128)
def _asset_bounds(path: Path, revision: tuple[int, int, int]):
    with path.open("rb") as stream:
        return stl_bounds(stream.read(MAX_STL_BYTES + 1))


def asset_bounds(asset: str):
    path = asset_path(asset)
    stat = path.stat()
    return _asset_bounds(path, (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


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


def asset_references(data: object) -> set[Path]:
    if not isinstance(data, (list, tuple)):
        raise ValueError("meshes must be a list")
    result = set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("asset"), str):
            raise ValueError("Each fixture must name its asset")
        result.add(asset_path(item["asset"]))
    return result


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
        bounds = asset_bounds(asset)
        mesh_size(bounds, scale)
        result.append(FixtureMesh(asset, scale, pose, name, tuple(members), bounds))
        names.add(name)
        proxies.update(members)
    return tuple(result)


def restore_meshes(data: object):
    """Keep unreadable records intact so an ordinary edit cannot erase them."""
    if not isinstance(data, (list, tuple)):
        return (
            (),
            [data],
            ["Invalid fixture manifest; repair scene_fixture_meshes in storage"],
        )
    meshes, unavailable, errors = [], [], []
    names, proxies = set(), set()
    for item in data:
        try:
            fixture = meshes_from_data([item])[0]
            if fixture.name in names or proxies.intersection(fixture.proxy_names):
                raise ValueError("Fixture names and proxy memberships must be unique")
            meshes.append(fixture)
            names.add(fixture.name)
            proxies.update(fixture.proxy_names)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            unavailable.append(item)
            errors.append(
                f"{item.get('name', 'Fixture') if isinstance(item, dict) else 'Fixture'}: {error}"
            )
    return tuple(meshes), unavailable, errors
