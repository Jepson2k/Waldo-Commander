"""Temporary authenticated loopback access to a program's Commander camera."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from collections.abc import Awaitable, Callable

from waldo_commander.camera import CameraSnapshot, CameraUnavailable
from waldo_commander.camera_sources import MAX_IMAGE_BYTES, check_timeout

logger = logging.getLogger(__name__)

# How long closing waits for handlers and the listening socket to finish.
CLOSE_TIMEOUT_S = 1.0


class CameraSession:
    def __init__(self, capture: Callable[..., Awaitable[CameraSnapshot]]) -> None:
        self.capture = capture
        self.token = secrets.token_urlsafe(32)
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        self._closing: asyncio.Future[None] | None = None
        self._slots = asyncio.Semaphore(2)

    async def start(self) -> dict[str, str]:
        if self.server is not None or self.closed:
            raise RuntimeError("A camera session can only be started once")
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, limit=4096
        )
        port = self.server.sockets[0].getsockname()[1]
        return {
            "WALDO_CAMERA_ENDPOINT": f"127.0.0.1:{port}",
            "WALDO_CAMERA_TOKEN": self.token,
        }

    async def close(self) -> None:
        """Stop serving; a request waiting on a stalled camera or on a peer
        that stopped reading is cut off rather than waited out.

        Every caller waits for the same close, however many there are.
        """
        if self._closing is None:
            self.closed = True
            self._closing = asyncio.ensure_future(self._shut_down())
        await asyncio.shield(self._closing)

    async def _shut_down(self) -> None:
        if self.server is not None:
            self.server.close()
        # Handlers abort their connections once the session is closed, which
        # is what lets wait_closed() return.
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT_S)
        if self.server is not None:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await self.server.wait_closed()
            except TimeoutError:
                logger.warning(
                    "Camera session connections outlived %.1f s", CLOSE_TIMEOUT_S
                )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            async with asyncio.timeout(6):
                request = json.loads(await reader.readline())
                if (
                    not isinstance(request, dict)
                    or not isinstance(request.get("token"), str)
                    or not secrets.compare_digest(request["token"], self.token)
                ):
                    response = {"error": "Camera session authorization failed"}
                else:
                    if set(request) != {"token", "timeout_s"}:
                        raise ValueError("Invalid camera request fields")
                    timeout_s = request.get("timeout_s", 1.0)
                    check_timeout(timeout_s)
                    async with asyncio.timeout(timeout_s):
                        async with self._slots:
                            observation = await self.capture(timeout_s=timeout_s)
                    if len(observation.jpeg) > MAX_IMAGE_BYTES:
                        raise CameraUnavailable("Camera image exceeds 8 MiB")
                    response = {
                        "jpeg": base64.b64encode(observation.jpeg).decode("ascii"),
                        "camera_id": observation.camera_id,
                        "received_at": observation.received_at,
                        "sequence": observation.sequence,
                        "session_id": observation.session_id,
                    }
                writer.write(json.dumps(response, allow_nan=False).encode() + b"\n")
                await writer.drain()
        except (CameraUnavailable, ValueError, TypeError, TimeoutError) as error:
            try:
                writer.write(
                    json.dumps(
                        {"error": str(error) or "Camera acquisition deadline expired"}
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
            except OSError:
                pass
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if self.closed:
                writer.transport.abort()
            else:
                writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.tasks.discard(task)
