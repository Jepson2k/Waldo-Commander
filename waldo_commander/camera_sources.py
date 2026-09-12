"""Explicit camera sources for programs; importing this module starts nothing."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from waldoctl.setup import Pose, TcpCalibration

from waldo_commander.camera import CameraSnapshot, CameraUnavailable

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024


def check_timeout(timeout_s: float) -> None:
    if (
        isinstance(timeout_s, bool)
        or not math.isfinite(timeout_s)
        or not 0 < timeout_s <= 5
    ):
        raise ValueError("Camera acquisition timeout must be in (0, 5] seconds")


class FrameSource(Protocol):
    async def snapshot(self, *, timeout_s: float = 1.0) -> CameraSnapshot:
        """Return an independently owned, fresh host-timestamped image."""
        ...


@dataclass(frozen=True)
class ImageFixture:
    """Explicit image and optional tool observation for sensor-dependent preview."""

    observation: CameraSnapshot
    tcp_pose: Pose | None = None
    tool: TcpCalibration | None = None

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        camera_id: str,
        tcp_pose: Pose | None = None,
        tool: TcpCalibration | None = None,
    ) -> ImageFixture:
        image = Path(path).read_bytes()
        if len(image) > MAX_IMAGE_BYTES:
            raise ValueError("Fixture image exceeds 8 MiB")
        return cls(CameraSnapshot(image, camera_id, 0.0, 1, "fixture"), tcp_pose, tool)

    async def snapshot(self, *, timeout_s: float = 1.0) -> CameraSnapshot:
        check_timeout(timeout_s)
        return self.observation


@dataclass(frozen=True)
class CommanderCameraSource:
    """Request a fresh image from the Commander session that launched a program.

    Construction performs no I/O. Standalone programs can implement FrameSource
    using their own camera; this source requires a live Commander session.
    """

    endpoint: str | None = None
    token: str | None = field(default=None, repr=False)

    async def snapshot(self, *, timeout_s: float = 1.0) -> CameraSnapshot:
        check_timeout(timeout_s)
        endpoint = self.endpoint or os.environ.get("WALDO_CAMERA_ENDPOINT")
        token = self.token or os.environ.get("WALDO_CAMERA_TOKEN")
        if not endpoint or not token:
            raise CameraUnavailable(
                "No Commander camera session; supply your own FrameSource in standalone programs"
            )
        host, separator, port_text = endpoint.rpartition(":")
        if (
            separator != ":"
            or host != "127.0.0.1"
            or not port_text.isdecimal()
            or not 0 < int(port_text) < 65536
        ):
            raise CameraUnavailable("Invalid Commander camera session endpoint")
        writer = None
        try:
            async with asyncio.timeout(timeout_s + 0.5):
                reader, writer = await asyncio.open_connection(
                    host, int(port_text), limit=MAX_RESPONSE_BYTES
                )
                writer.write(
                    json.dumps({"token": token, "timeout_s": timeout_s}).encode()
                    + b"\n"
                )
                await writer.drain()
                response = json.loads(await reader.readline())
                if not isinstance(response, dict):
                    raise ValueError("Expected a camera response")
                if "error" in response:
                    raise CameraUnavailable(str(response["error"]))
                if set(response) != {
                    "jpeg",
                    "camera_id",
                    "received_at",
                    "sequence",
                    "session_id",
                }:
                    raise ValueError("Invalid camera response fields")
                image = base64.b64decode(response.pop("jpeg"), validate=True)
                if len(image) > MAX_IMAGE_BYTES:
                    raise ValueError("Camera image exceeds 8 MiB")
                observation = CameraSnapshot(jpeg=image, **response)
                age = time.time() - observation.received_at
                if not -0.5 <= age <= timeout_s + 0.5:
                    raise CameraUnavailable(
                        "Camera response is stale or its clock does not match"
                    )
                return observation
        except (OSError, ValueError, TypeError) as error:
            raise CameraUnavailable(
                f"Camera session could not provide an image: {error}"
            ) from error
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
