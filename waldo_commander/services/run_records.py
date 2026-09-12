"""Local opt-in execution journals and conservative debugging exports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import uuid4

from waldoctl.record_values import snapshot_value

logger = logging.getLogger(__name__)
MAX_RECORD_BYTES = 4 * 1024 * 1024

#: Bumped when an event's fields change meaning. A journal is read by a
#: Commander that may be newer or older than the one that wrote it, so the
#: label travels in the first entry and the reader refuses what it cannot
#: read rather than rendering the wrong field.
RECORD_SCHEMA = 1


def record_directory() -> Path:
    return Path(
        os.environ.get(
            "WALDO_RUN_RECORD_DIR", str(Path.home() / ".waldo-commander" / "runs")
        )
    )


def package_versions() -> dict[str, str]:
    result = {}
    for package in ("waldo-commander", "waldoctl", "parol6", "par6"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            continue
    return result


class RunRecord:
    """One bounded journal; a disk failure disables capture, never execution."""

    def __init__(self, source: str, backend: str, *, directory: Path | None = None):
        self.id = uuid4().hex
        self.path = (directory or record_directory()) / f"{self.id}.jsonl"
        self.bytes_written = 0
        self.truncated = False
        self.error: str | None = None
        self.finished = False
        self._status_task: asyncio.Task | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Private local values are never world-readable on POSIX.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        self.append(
            {
                "event": "run_started",
                "schema": RECORD_SCHEMA,
                "run_id": self.id,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "backend": backend,
                "versions": package_versions(),
            }
        )

    def append(self, event: dict[str, Any]) -> None:
        if self.error or self.finished:
            return
        entry = {
            "received_ns": time.monotonic_ns(),
            "received_at": time.time(),
            **snapshot_value(event),
        }
        encoded = (
            json.dumps(entry, allow_nan=False, separators=(",", ":")) + "\n"
        ).encode()
        if self.truncated and event.get("event") != "run_finished":
            return
        if (
            self.bytes_written + len(encoded) > MAX_RECORD_BYTES - 2048
            and event.get("event") != "run_finished"
        ):
            self.truncated = True
            encoded = (
                json.dumps(
                    {"event": "record_truncated", "received_ns": time.monotonic_ns()}
                )
                + "\n"
            ).encode()
        try:
            with self.path.open("ab") as stream:
                stream.write(encoded)
            self.bytes_written += len(encoded)
        except OSError as error:
            self.error = str(error)
            logger.exception("Run recording stopped after a storage error")

    def start_status(self, client: Any) -> None:
        async def collect():
            next_sample = 0.0
            previous = None
            try:
                async for status in client.stream_status():
                    now = time.monotonic()
                    state = (
                        status.session_id,
                        status.enabled,
                        status.homed,
                        status.collision_active,
                    )
                    if now < next_sample and state == previous:
                        continue
                    previous = state
                    next_sample = now + 0.5
                    self.append(
                        {
                            "event": "status",
                            "snapshot": snapshot_value(
                                {
                                    "session_id": status.session_id,
                                    "seq": status.seq,
                                    "mono_time_ns": status.mono_time_ns,
                                    "homed": status.homed,
                                    "enabled": status.enabled,
                                    "angles_deg": status.angles,
                                    "speeds_rad_s": status.speeds,
                                    "executing_index": status.executing_index,
                                    "completed_index": status.completed_index,
                                    "scene_epoch": status.scene_epoch,
                                    "collision_active": status.collision_active,
                                    "drive_health": status.drive_health,
                                    "link_health": status.link_health,
                                }
                            ),
                        }
                    )
                    if self.truncated or self.error:
                        return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.append(
                    {"event": "status_unavailable", "error_type": type(error).__name__}
                )

        self._status_task = asyncio.create_task(collect())

    async def capture_context(self, client: Any) -> None:
        """Bounded readbacks before launch; individual observation times are kept."""
        try:
            async with asyncio.timeout(3.0):
                for name in (
                    "tools",
                    "tcp_transform",
                    "payload",
                    "profile",
                    "shapes",
                    "execution_speed",
                ):
                    started = time.monotonic_ns()
                    try:
                        value = await getattr(client, name)()
                    except Exception as error:
                        self.append(
                            {
                                "event": "context_unavailable",
                                "method": name,
                                "error_type": type(error).__name__,
                            }
                        )
                    else:
                        self.append(
                            {
                                "event": "controller_context",
                                "method": name,
                                "query_started_ns": started,
                                "result": snapshot_value(value),
                            }
                        )
        except TimeoutError:
            self.append({"event": "context_unavailable", "error_type": "TimeoutError"})

    def finish(self, outcome: str, exit_code: int | None = None) -> None:
        if self._status_task is not None:
            self._status_task.cancel()
            self._status_task = None
        self.append(
            {"event": "run_finished", "outcome": outcome, "exit_code": exit_code}
        )
        self.finished = True


def load_record(path: Path) -> list[dict[str, Any]]:
    """Read a bounded journal, preserving complete entries after an abrupt exit.

    A journal written to another schema is refused by name: its events carry
    the same keys with different meanings, so reading it anyway would show
    wrong values as confidently as right ones.
    """
    with path.open("rb") as stream:
        content = stream.read(MAX_RECORD_BYTES + 1)
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError("Run record exceeds the supported size")
    events = []
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if i == len(lines) - 1 and not line.endswith(b"\n"):
                break
            raise ValueError("Invalid run record entry") from None
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            raise ValueError("Invalid run record entry")
        if event["event"] == "run_started" and event.get("schema") != RECORD_SCHEMA:
            raise ValueError(
                f"Run record schema {event.get('schema')!r} is not this "
                f"Commander's ({RECORD_SCHEMA}); open it with the version that "
                f"wrote it"
            )
        events.append(event)
    return events


# Only these schema labels survive inside arbitrary captured values. User
# mapping keys and all string values are removed, including paths and URLs.
_FIELDS = frozenset(
    "positions values scale target_scale applied_scale resume_scale speed accel torque timeout wait r args kwargs angles pose frames poses parameters signals cameras frame parent schema_version shape mass collision physics xyz rpy translation rotation index code enabled homed angles_deg speeds_rad_s session_id seq mono_time_ns executing_index completed_index scene_epoch collision_active drive_health link_health temperatures_c currents_ma bus_voltage_v faults state restarts tx_errors rx_frames installation program attachment_epoch".split()
)


def _share_value(value: Any) -> Any:
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, list):
        return [_share_value(v) for v in value]
    if isinstance(value, dict):
        return {
            (k if k in _FIELDS else f"field_{i}"): _share_value(v)
            for i, (k, v) in enumerate(value.items())
        }
    return "<omitted>"


def debugging_export(path: Path) -> bytes:
    """Export numeric/structural diagnostics without source, logs or free text."""
    aliases: dict[str, str] = {}
    events = []
    from .stepping_client import (
        STEPPABLE_METHODS,
        _EXECUTION_CONTROLS,
        _STEPPABLE_TOOL_METHODS,
    )

    methods = (
        STEPPABLE_METHODS
        | {f"tool.{name}" for name in _STEPPABLE_TOOL_METHODS}
        | _EXECUTION_CONTROLS
        | {
            "wait_command",
            "load_setup",
            "blend_group",
            "tools",
            "tcp_transform",
            "payload",
            "profile",
            "shapes",
        }
    )
    for event in load_record(path):
        kind = event["event"]
        if kind not in {
            "run_started",
            "run_finished",
            "status",
            "status_unavailable",
            "controller_context",
            "context_unavailable",
            "record_truncated",
            "events_lost",
            "setup_loaded",
            "start",
            "complete",
            "pause",
            "resume",
            "step",
        } and not re.fullmatch(
            r"(?:skill|command)_(?:started|progress|completed|returned|failed|cancelled|unconfirmed|wait_failed)",
            kind,
        ):
            continue
        clean = {"event": kind}
        for key in (
            "step",
            "sequence",
            "count",
            "ts",
            "mono_ns",
            "active_s",
            "received_ns",
            "received_at",
            "fraction",
            "stop_confirmed",
            "index",
            "code",
            "exit_code",
            "query_started_ns",
        ):
            if key in event and (
                event[key] is None or type(event[key]) in (int, float, bool)
            ):
                clean[key] = event[key]
        for key in ("command_id", "invocation_id", "parent_id"):
            if isinstance(event.get(key), str):
                clean[key] = aliases.setdefault(
                    event[key], f"invocation_{len(aliases) + 1}"
                )
        if isinstance(event.get("method"), str):
            name = event["method"]
            clean["method"] = (
                name
                if name in methods
                else aliases.setdefault(name, f"custom_{len(aliases) + 1}")
            )
        for key in ("arguments", "result", "snapshot"):
            if key in event:
                clean[key] = _share_value(event[key])
        if event.get("outcome") in {
            "completed",
            "failed",
            "stopped",
            "stop_unconfirmed",
            "start_failed",
            "interrupted",
        }:
            clean["outcome"] = event["outcome"]
        if kind == "run_started":
            digest = event.get("source_sha256")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                clean["source_sha256"] = digest
            clean["versions"] = {
                k: v
                for k, v in event.get("versions", {}).items()
                if k in {"waldo-commander", "waldoctl", "parol6", "par6"}
                and isinstance(v, str)
                and re.fullmatch(r"[0-9][0-9a-zA-Z.+-]{0,79}", v)
            }
        events.append(clean)
    return (
        json.dumps(
            {"schema": RECORD_SCHEMA, "export": "numeric-debug", "events": events},
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode()
