"""Camera feed service for gripper panel live view.

Captures frames from a video device and streams them as MJPEG via a
``multipart/x-mixed-replace`` HTTP endpoint.  The browser renders the
stream natively in an ``<img>`` tag — no JavaScript polling required.

On Linux the service tries ``linuxpy`` first for zero-copy MJPEG
passthrough (raw JPEG frames straight from v4l2, no decode+re-encode).
Falls back to OpenCV on other platforms or when ``linuxpy`` is
unavailable.

Typical workflow for AI annotations:
  physical webcam → user's overlay/analysis script → pyvirtualcam output
  → Web Commander reads the virtual camera device
"""

from __future__ import annotations

import asyncio
import base64
import logging
import hashlib
import json
import math
import time
import uuid
import sys
from typing import Protocol

from fastapi import Response
from starlette.responses import StreamingResponse

from nicegui import app as ng_app, run

from waldo_commander.camera import CameraSnapshot, CameraUnavailable

# Suppress linuxpy's verbose per-ioctl debug logging
logging.getLogger("linuxpy").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_BLACK_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAAXNSR0IArs4c6Q"
    "AAAA1JREFUGFdjYGBg+A8AAQQBAHAgZQsAAAAASUVORK5CYII="
)
_PLACEHOLDER = Response(content=_BLACK_1PX, media_type="image/png")

_STREAM_FPS = 30
_STREAM_BOUNDARY = b"frame"


class CaptureBackend(Protocol):
    """Minimal interface for a camera capture backend."""

    def open(self, device: int | str, width: int, height: int) -> bool: ...
    def read_frame(self) -> bytes | None: ...
    def close(self) -> None: ...


class LinuxpyBackend:
    """Zero-copy MJPEG capture via linuxpy (v4l2).

    Frames come straight from the kernel as JPEG — no decode or
    re-encode step.  Only available on Linux.
    """

    def __init__(self) -> None:
        self._capture = None

    def open(self, device: int | str, width: int, height: int) -> bool:
        try:
            from linuxpy.video.device import BufferType, Device, VideoCapture
        except ImportError:
            logger.debug("linuxpy not installed — skipping v4l2 backend")
            return False

        if not isinstance(device, int):
            logger.debug("linuxpy requires integer device index, got %s", type(device))
            return False

        try:
            dev = Device(f"/dev/video{device}")
            dev.open()
            dev.set_format(BufferType.VIDEO_CAPTURE, width, height, pixel_format="MJPG")
            cap = VideoCapture(dev)
            cap.open()
            self._capture = cap
            logger.info(
                "linuxpy v4l2 backend opened /dev/video%d (%dx%d MJPG)",
                device,
                width,
                height,
            )
            return True
        except Exception:
            logger.debug("linuxpy failed to open device %s", device, exc_info=True)
            try:
                dev.close()
            except OSError:
                pass
            if self._capture is not None:
                try:
                    self._capture.close()
                except OSError:
                    pass
                self._capture = None
            return False

    def read_frame(self) -> bytes | None:
        """Read one MJPEG frame (blocking)."""
        if self._capture is None:
            return None
        try:
            for frame in self._capture:
                return bytes(frame.data)
        except Exception:
            logger.debug("linuxpy read_frame error", exc_info=True)
        return None

    def close(self) -> None:
        if self._capture is not None:
            try:
                self._capture.close()
            except OSError:
                pass
            self._capture = None


class OpenCVBackend:
    """Fallback backend using OpenCV.  Decodes + re-encodes to JPEG."""

    def __init__(self) -> None:
        self._cap = None

    def open(self, device: int | str, width: int, height: int) -> bool:
        try:
            import cv2
        except ImportError:
            logger.warning("opencv-python-headless not installed — camera disabled")
            return False

        cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            logger.warning("OpenCV failed to open camera device %s", device)
            cap.release()
            return False

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._cap = cap
        logger.info("OpenCV backend opened device %s (%dx%d)", device, width, height)
        return True

    def read_frame(self) -> bytes | None:
        """Read one frame and return JPEG bytes (blocking)."""
        import cv2

        if self._cap is None or not self._cap.isOpened():
            return None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes()

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class CameraService:
    """Manages a single camera and caches the latest JPEG frame."""

    def __init__(self) -> None:
        self._backend: CaptureBackend | None = None
        self._latest_jpeg: bytes = _BLACK_1PX
        self._active: bool = False
        self._capture_task: asyncio.Task | None = None
        self._snapshot: CameraSnapshot | None = None
        self._received_monotonic = 0.0
        self._camera_id: str | None = None
        self._session_id = ""
        self._device: int | str | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def device(self) -> int | str | None:
        """The device the running capture was opened on."""
        return self._device if self._active else None

    @property
    def camera_id(self) -> str | None:
        return self._camera_id

    def snapshot(self, *, max_age_s: float = 1.0) -> CameraSnapshot:
        """Return a fresh host-received image; an idle placeholder is never data."""
        if (
            isinstance(max_age_s, bool)
            or not math.isfinite(max_age_s)
            or max_age_s <= 0
        ):
            raise ValueError("Camera max_age_s must be finite and positive")
        if not self._active or self._snapshot is None:
            raise CameraUnavailable("No camera frame available")
        if time.monotonic() - self._received_monotonic > max_age_s:
            raise CameraUnavailable("Camera frame is stale")
        return self._snapshot

    async def next_snapshot(self, *, timeout_s: float = 1.0) -> CameraSnapshot:
        """Wait for a subsequent host receipt within the current capture session."""
        if (
            isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("Camera timeout_s must be finite and positive")
        if not self._active:
            raise CameraUnavailable("No camera active")
        session = self._session_id
        sequence = self._snapshot.sequence if self._snapshot else 0
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self._active or self._session_id != session:
                raise CameraUnavailable("Camera source changed during capture")
            if self._snapshot is not None and self._snapshot.sequence > sequence:
                return self.snapshot(max_age_s=timeout_s)
            await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        raise CameraUnavailable("No new camera frame arrived before the deadline")

    def start(self, device: int | str, width: int = 640, height: int = 480) -> None:
        """Open a camera device and begin capturing."""
        self.stop()

        backend: CaptureBackend | None = None

        if sys.platform == "linux" and isinstance(device, int):
            candidate = LinuxpyBackend()
            if candidate.open(device, width, height):
                backend = candidate

        if backend is None:
            candidate_cv = OpenCVBackend()
            if candidate_cv.open(device, width, height):
                backend = candidate_cv

        if backend is None:
            logger.warning("No camera backend could open device %s", device)
            return

        self._backend = backend
        self._active = True
        self._device = device
        descriptor = json.dumps([type(device).__name__, device, width, height])
        self._camera_id = (
            "capture-" + hashlib.sha256(descriptor.encode()).hexdigest()[:24]
        )
        self._session_id = uuid.uuid4().hex
        self._capture_task = asyncio.get_event_loop().create_task(
            self._capture_loop(backend, self._camera_id, self._session_id)
        )
        logger.info("Camera started on device %s", device)

    def stop(self) -> None:
        """Release the camera device and stop the capture loop."""
        self._active = False
        self._snapshot = None
        self._camera_id = None
        self._session_id = ""
        if self._capture_task is not None:
            self._capture_task.cancel()
            self._capture_task = None
        if self._backend is not None:
            self._backend.close()
            self._backend = None
        self._latest_jpeg = _BLACK_1PX

    def get_latest_frame(self) -> bytes:
        """Return the most recently captured JPEG (non-blocking)."""
        return self._latest_jpeg

    async def _capture_loop(
        self, backend: CaptureBackend, camera_id: str, session_id: str
    ) -> None:
        """Background task: read frames from the backend at ~30 fps."""
        interval = 1.0 / _STREAM_FPS
        sequence = 0
        try:
            while self._active and self._session_id == session_id:
                frame = await run.io_bound(backend.read_frame)
                if self._session_id != session_id:
                    return
                if frame is not None:
                    sequence += 1
                    self._latest_jpeg = frame
                    self._received_monotonic = time.monotonic()
                    self._snapshot = CameraSnapshot(
                        frame, camera_id, time.time(), sequence, session_id
                    )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.error("Capture loop crashed", exc_info=True)
            if self._session_id == session_id:
                self._active = False
                self._snapshot = None


# Module-level singleton
camera_service = CameraService()


def enumerate_video_devices(max_check: int = 10) -> list[dict[str, int | str]]:
    """Detect available video capture devices.

    On Linux, uses linuxpy/v4l2 to check device capabilities (avoids
    OpenCV warnings and correctly skips metadata-only nodes). On Windows
    and macOS, uses cv2-enumerate-cameras (MSMF / AVFoundation device
    listing) so cameras are never opened just to be listed.

    Returns a list of ``{"index": int, "label": str}`` dicts.
    """
    if sys.platform == "linux":
        devs = _enumerate_v4l2(max_check)
        if devs is None:
            logger.warning(
                "linuxpy not installed — camera listing needs the [v4l2] extra"
            )
            return []
        return devs
    devs = _enumerate_listing()
    if devs is None:
        logger.error("cv2 / cv2-enumerate-cameras not installed — cannot list cameras")
        return []
    return devs


def _enumerate_v4l2(max_check: int) -> list[dict[str, int | str]] | None:
    """List v4l2 devices that support VIDEO_CAPTURE. Returns None if linuxpy unavailable."""
    try:
        from linuxpy.video.device import Capability, Device
    except ImportError:
        return None

    devices: list[dict[str, int | str]] = []
    for i in range(max_check):
        path = f"/dev/video{i}"
        try:
            dev = Device(path)
            dev.open()
            try:
                info = dev.info
                if info is None:
                    continue
                # Prefer device_capabilities (per-node) over capabilities (driver-wide)
                caps = info.device_capabilities or info.capabilities
                if Capability.VIDEO_CAPTURE in caps:
                    card = info.card
                    label = card if card else f"Camera {i}"
                    devices.append({"index": i, "label": label})
            finally:
                dev.close()
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("v4l2 probe failed for %s", path, exc_info=True)
    return devices


def _enumerate_listing() -> list[dict[str, int | str]] | None:
    """List cameras from the OS device registry without opening capture streams.

    Returns None when cv2-enumerate-cameras is unavailable (it is only
    installed on Windows/macOS).
    """
    try:
        import cv2
        from cv2_enumerate_cameras import enumerate_cameras  # ty: ignore[unresolved-import]
    except ImportError:
        return None
    # MSMF / AVFoundation are what VideoCapture(index) resolves to on these
    # platforms, so listing indices line up with capture indices.
    if sys.platform == "win32":
        backend = cv2.CAP_MSMF
    elif sys.platform == "darwin":
        backend = cv2.CAP_AVFOUNDATION
    else:
        return None
    return [
        {"index": info.index, "label": info.name or f"Camera {info.index}"}
        for info in enumerate_cameras(backend)
    ]


async def _mjpeg_generator():
    """Yield MJPEG multipart frames at ~30 fps."""
    interval = 1.0 / _STREAM_FPS
    try:
        while camera_service.active:
            jpeg = camera_service.get_latest_frame()
            yield (
                b"--" + _STREAM_BOUNDARY + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n"
                b"\r\n" + jpeg + b"\r\n"
            )
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return


async def _tool_camera_stream():
    """Serve the camera feed as an MJPEG multipart stream."""
    if not camera_service.active:
        return _PLACEHOLDER
    return StreamingResponse(
        _mjpeg_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={_STREAM_BOUNDARY.decode()}",
        headers={"Cache-Control": "no-cache"},
    )


async def _tool_camera_frame() -> Response:
    """Serve a single JPEG snapshot."""
    if not camera_service.active:
        return _PLACEHOLDER
    return Response(content=camera_service.get_latest_frame(), media_type="image/jpeg")


def register_camera_routes() -> None:
    """Register endpoints on the current app, including after an app rebuild."""
    paths = {getattr(route, "path", None) for route in ng_app.routes}
    for path, endpoint in (
        ("/tool/camera/stream", _tool_camera_stream),
        ("/tool/camera/frame", _tool_camera_frame),
    ):
        if path not in paths:
            ng_app.get(path, response_model=None)(endpoint)
