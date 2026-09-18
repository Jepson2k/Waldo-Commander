"""Portable camera observations for explicit acquisition sources."""

from dataclasses import dataclass


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
