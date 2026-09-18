"""Portable camera observations for explicit acquisition sources."""

from dataclasses import dataclass
import math


class CameraUnavailable(RuntimeError):
    """The source cannot provide a fresh image."""


@dataclass(frozen=True)
class CameraSnapshot:
    """One encoded image, timestamped on host receipt (not hardware exposure).

    Sequence numbers are local to a capture session. Programs should pair
    images with stationary robot observations unless they have an independently
    synchronized capture source.
    """

    jpeg: bytes
    camera_id: str
    received_at: float
    sequence: int
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.jpeg, bytes) or not self.jpeg:
            raise ValueError(
                "A camera snapshot requires nonempty immutable image bytes"
            )
        if any(
            not isinstance(value, str) or not value or len(value) > 256
            for value in (self.camera_id, self.session_id)
        ):
            raise ValueError(
                "Camera and session identities must be nonempty strings of at most 256 characters"
            )
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("Camera sequence must be a nonnegative integer")
        if (
            isinstance(self.received_at, bool)
            or not isinstance(self.received_at, (int, float))
            or not math.isfinite(self.received_at)
            or self.received_at < 0
        ):
            raise ValueError(
                "Camera receipt time must be a finite nonnegative Unix timestamp"
            )
