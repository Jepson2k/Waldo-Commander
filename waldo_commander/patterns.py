"""Pose patterns and explicit, advisory completion records for Python loops."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from waldoctl.setup import Pose

MAX_CELLS = 100_000


class _ExecutionContext(Protocol):
    @property
    def skill_capabilities(self) -> frozenset[str]: ...


def _number(value: object) -> bool:
    """A finite real measurement. Typed first: a pattern built from
    configuration or CSV data arrives as text, and math.isfinite on text
    raises TypeError where these messages promise a ValueError."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def offset_poses(
    origin: Pose, offsets_mm: Iterable[Sequence[float]]
) -> tuple[Pose, ...]:
    """Translate in the origin's reference-frame axes, retaining TCP orientation."""
    poses = []
    for offset in offsets_mm:
        if len(poses) >= MAX_CELLS:
            raise ValueError(f"A pattern may contain at most {MAX_CELLS} poses")
        if len(offset) != 3 or any(not _number(v) for v in offset):
            raise ValueError("Offsets require three finite millimeter values")
        poses.append(
            Pose(
                (*(origin.values[i] + offset[i] for i in range(3)), *origin.values[3:]),
                origin.frame,
            )
        )
    return tuple(poses)


def grid_poses(
    origin: Pose,
    *,
    rows: int,
    columns: int,
    pitch_x_mm: float,
    pitch_y_mm: float,
    layers: int = 1,
    pitch_z_mm: float = 0.0,
    serpentine: bool = False,
) -> tuple[Pose, ...]:
    """Rows along reference Y, columns along X, layers along Z; X varies first.

    Negative pitch reverses an axis. Tool orientation does not rotate the
    grid; use a named setup frame for a rotated or tilted tray.
    """
    if (
        any(type(n) is not int or n < 1 for n in (rows, columns, layers))
        or rows * columns * layers > MAX_CELLS
    ):
        raise ValueError(
            f"Positive whole-number dimensions must total at most {MAX_CELLS} cells"
        )
    if type(serpentine) is not bool:
        raise ValueError("serpentine must be a boolean")
    for pitch, count in (
        (pitch_x_mm, columns),
        (pitch_y_mm, rows),
        (pitch_z_mm, layers),
    ):
        if not _number(pitch) or (count > 1 and pitch == 0):
            raise ValueError("Pitch must be finite and nonzero for a repeated axis")
    offsets = (
        (column * pitch_x_mm, row * pitch_y_mm, layer * pitch_z_mm)
        for layer in range(layers)
        for row in range(rows)
        for column in (
            reversed(range(columns)) if serpentine and row % 2 else range(columns)
        )
    )
    return offset_poses(origin, offsets)


def _pattern_identity(poses: Sequence[Pose]) -> tuple[str, int]:
    if not 0 < len(poses) <= MAX_CELLS or any(pose.frame != "WRF" for pose in poses):
        raise ValueError(
            "Progress requires a nonempty, bounded pattern resolved to WRF"
        )
    data = json.dumps(
        [pose.values for pose in poses], allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(data.encode()).hexdigest(), len(poses)


@dataclass(frozen=True)
class PatternProgress:
    """User-editable notes about completed indices, never permission to resume.

    A completed command sequence does not confirm a grasp or a placed part.
    Inspect the physical tray and reconcile indices after an interruption.
    """

    pattern_digest: str
    size: int
    completed: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.pattern_digest, str)
            or len(self.pattern_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.pattern_digest)
        ):
            raise ValueError("Progress needs a SHA-256 pattern identity")
        if type(self.size) is not int or not 0 < self.size <= MAX_CELLS:
            raise ValueError("Invalid progress size")
        indices = tuple(self.completed)
        if any(type(i) is not int or not 0 <= i < self.size for i in indices) or len(
            set(indices)
        ) != len(indices):
            raise ValueError(
                "Completed indices must be unique integers within the pattern"
            )
        object.__setattr__(self, "completed", frozenset(indices))

    @classmethod
    def for_poses(cls, poses: Sequence[Pose]) -> PatternProgress:
        return cls(*_pattern_identity(poses))

    def mark(self, index: int, *, completed: bool = True) -> PatternProgress:
        if (
            type(index) is not int
            or not 0 <= index < self.size
            or type(completed) is not bool
        ):
            raise ValueError(
                "Choose an index within this pattern and a boolean completion value"
            )
        return replace(
            self,
            completed=self.completed | {index}
            if completed
            else self.completed - {index},
        )

    def pending(self) -> tuple[int, ...]:
        return tuple(index for index in range(self.size) if index not in self.completed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "pattern_digest": self.pattern_digest,
            "size": self.size,
            "completed": sorted(self.completed),
        }


def load_progress(path: str | Path, poses: Sequence[Pose]) -> PatternProgress:
    """Read advisory progress only if the resolved poses and their order match."""
    with Path(path).open("r", encoding="utf-8") as stream:
        text = stream.read(2 * 1024 * 1024 + 1)
    if len(text) > 2 * 1024 * 1024:
        raise ValueError("Progress document exceeds 2 MiB")
    document = json.loads(text)
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "pattern_digest", "size", "completed"}
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise ValueError("Unsupported pattern progress document")
    if not isinstance(document["completed"], list):
        raise ValueError("Progress completed indices must be a list")
    progress = PatternProgress(
        document["pattern_digest"], document["size"], document["completed"]
    )
    if (progress.pattern_digest, progress.size) != _pattern_identity(poses):
        raise ValueError(
            "Pattern or setup changed; reconcile progress against the new poses"
        )
    return progress


def save_progress(
    path: str | Path,
    progress: PatternProgress,
    *,
    client: _ExecutionContext,
) -> bool:
    """Atomically save explicit progress; return False without writing in preview.

    Call after a successful transfer, passing the same client used to execute
    it. There is no implicit loading, skipping, reset, or recovery move.
    """
    if "execution.preview" in client.skill_capabilities:
        return False
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(progress.to_dict(), stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True
