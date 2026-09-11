"""Calibration section of the Diagnostics tab — par6 arms only.

Runs one `par6.calibration` routine against the runtime Commander manages,
streams its progress, shows the staged patch, and installs or rolls back the
candidate config for the next runtime start. Nothing here is a waldoctl
contract: the section only appears when the active backend is PAR6 and the
par6 calibration package is importable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import waldoctl
from nicegui import background_tasks, ui
from waldoctl.robot_status import ActionState

from waldo_commander.services.control_lease import require_browser_control
from waldo_commander.state import robot_state, ui_state

logger = logging.getLogger(__name__)

ROUTINES = {
    "check": "Check (2 min baseline)",
    "tune-feedback": "Tune feedback gains (one joint)",
    "gravity": "Identify gravity model",
    "verify-gravity": "Verify applied gravity model",
    "limits": "Find joint limits (one-off)",
}

#: Where the arm's own config, its rollback and the runtime recordings live.
#: The managed par6d starts from `PAR6.toml` here when the file exists and
#: PAR6_CONFIG is not set by the user.
restart_runtime: Callable[[], Awaitable[None]] | None = None


def calibration_dir() -> Path:
    return Path(
        os.environ.get("WALDO_PAR6_DIR", str(Path.home() / ".waldo-commander" / "par6"))
    )


def available() -> bool:
    robot = ui_state.active_robot
    return (
        robot is not None
        and getattr(robot, "name", "").upper() == "PAR6"
        and importlib.util.find_spec("par6.calibration") is not None
    )


def prepare_managed_runtime_env() -> None:
    """Defaults for a Commander-managed par6d: record natively, and start from
    the installed calibration config when there is one. User-set values win."""
    if not available():
        return
    directory = calibration_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.environ.get("PAR6_DIAGNOSTICS"):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.environ["PAR6_DIAGNOSTICS"] = str(directory / f"capture-{stamp}.bin")
    installed = directory / "PAR6.toml"
    if not os.environ.get("PAR6_CONFIG") and installed.is_file():
        os.environ["PAR6_CONFIG"] = str(installed)


def install_config(candidate: Path) -> Path:
    """Copy a staged candidate (and its grippers) into the calibration dir,
    keeping the previous installed config as `rollback/`."""
    directory = calibration_dir()
    current = directory / "PAR6.toml"
    if current.is_file():
        rollback = directory / "rollback"
        shutil.rmtree(rollback, ignore_errors=True)
        rollback.mkdir(parents=True, mode=0o700)
        shutil.copy2(current, rollback / "PAR6.toml")
        if (directory / "grippers").is_dir():
            shutil.copytree(directory / "grippers", rollback / "grippers")
    shutil.copy2(candidate, current)
    grippers = candidate.parent / "grippers"
    if grippers.is_dir():
        shutil.rmtree(directory / "grippers", ignore_errors=True)
        shutil.copytree(grippers, directory / "grippers")
    os.environ["PAR6_CONFIG"] = str(current)
    return current


def rollback_config() -> Path | None:
    directory = calibration_dir()
    previous = directory / "rollback" / "PAR6.toml"
    if not previous.is_file():
        return None
    shutil.copy2(previous, directory / "PAR6.toml")
    if (directory / "rollback" / "grippers").is_dir():
        shutil.rmtree(directory / "grippers", ignore_errors=True)
        shutil.copytree(directory / "rollback" / "grippers", directory / "grippers")
    return directory / "PAR6.toml"


class CalibrationSection:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._task: asyncio.Task | None = None
        self._candidate: Path | None = None
        self._session: Any = None

    def build(self) -> None:
        with ui.column().classes("w-full gap-1").mark("diag-calibration"):
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                self.routine = (
                    ui.select(ROUTINES, value="check", label="Routine")
                    .props("dense options-dense")
                    .classes("grow")
                    .mark("calibration-routine")
                )
                self.joint = (
                    ui.number("Joint", value=3, min=1, max=6, step=1, format="%d")
                    .props("dense")
                    .classes("w-16")
                    .mark("calibration-joint")
                )
                self.start_button = (
                    ui.button(icon="play_arrow", on_click=self.start)
                    .props("dense round flat")
                    .mark("calibration-start")
                )
                self.stop_button = (
                    ui.button(icon="stop", on_click=self.stop)
                    .props("dense round flat color=negative")
                    .mark("calibration-stop")
                )
                self.stop_button.set_visibility(False)
            self.status = (
                ui.label("").classes("text-xs font-mono").mark("calibration-status")
            )
            self.log = (
                ui.log(max_lines=200)
                .classes("w-full h-32 text-xs")
                .mark("calibration-log")
            )
            self.patch = (
                ui.code("", language="toml").classes("w-full").mark("calibration-patch")
            )
            self.patch.set_visibility(False)
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                self.apply_button = (
                    ui.button("Apply and restart runtime", on_click=self.apply)
                    .props("dense")
                    .mark("calibration-apply")
                )
                self.apply_button.set_visibility(False)
                self.rollback_button = (
                    ui.button("Roll back", on_click=self.rollback)
                    .props("dense flat")
                    .mark("calibration-rollback")
                )
                self.rollback_button.set_visibility(
                    (calibration_dir() / "rollback" / "PAR6.toml").is_file()
                )
        self.joint.bind_visibility_from(
            self.routine, "value", lambda v: v == "tune-feedback"
        )

    # ------------------------------------------------------------ running

    def _say(self, text: str) -> None:
        self.status.set_text(text)

    def _push(self, line: str) -> None:
        self.log.push(line)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            self._say("A routine is already running")
            return
        if not require_browser_control(ui_state.active_client_id):
            return
        if (
            waldoctl.commander.status.action.state != ActionState.IDLE
            or not robot_state.homed
        ):
            self._say("The arm must be homed and idle before calibrating")
            return
        capture = os.environ.get("PAR6_DIAGNOSTICS")
        if not capture or not Path(capture).is_file():
            self._say(
                "The runtime is not recording: restart Commander so its par6d starts "
                "with PAR6_DIAGNOSTICS set (the Calibration section sets it by default)"
            )
            return
        self.patch.set_visibility(False)
        self.apply_button.set_visibility(False)
        self._candidate = None
        self.log.clear()
        self.stop_button.set_visibility(True)
        self.start_button.set_visibility(False)
        routine = self.routine.value
        joint = int(self.joint.value or 3) - 1
        self._task = background_tasks.create(
            self._run(routine, joint, Path(capture)), name=f"calibration-{routine}"
        )

    async def _run(self, routine: str, joint: int, capture: Path) -> None:
        from par6.calibration import Session, routines

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = calibration_dir() / "runs" / f"{stamp}-{routine}"
        started = time.monotonic()
        self._say(f"{ROUTINES[routine]}: running…")
        try:
            async with Session(self.client, directory, capture) as session:
                self._session = session
                session.log = self._push
                if routine == "check":
                    report = await routines.check(session)
                elif routine == "tune-feedback":
                    report = await routines.tune_feedback(session, joint=joint)
                elif routine == "gravity":
                    report = await routines.gravity(session)
                elif routine == "verify-gravity":
                    report = await routines.gravity(session, verify_only=True)
                else:
                    report = await routines.limits(session)
            verdict = "PASS" if report["valid"] else "FAIL"
            self._say(
                f"{report['kind']}: {verdict} in {time.monotonic() - started:.0f} s — {directory}"
            )
            for reason in report["reasons"]:
                self._push(f"- {reason}")
            candidate = report.get("candidate_config")
            if candidate:
                self._candidate = Path(candidate)
                self.patch.set_content(
                    (
                        self._candidate.parent.parent / "calibration-patch.toml"
                    ).read_text()
                )
                self.patch.set_visibility(True)
                self.apply_button.set_visibility(True)
        except asyncio.CancelledError:
            self._say("Stopped; the arm was brought to rest")
        except Exception as exc:  # the session has already stopped the arm
            logger.exception("calibration %s failed", routine)
            self._say(f"{routine} failed: {type(exc).__name__}: {exc}")
        finally:
            self._session = None
            self.stop_button.set_visibility(False)
            self.start_button.set_visibility(True)

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------ applying

    async def apply(self) -> None:
        if self._candidate is None or not require_browser_control(
            ui_state.active_client_id
        ):
            return
        installed = install_config(self._candidate)
        self.rollback_button.set_visibility(
            (calibration_dir() / "rollback" / "PAR6.toml").is_file()
        )
        await self._restart(f"Installed {installed}; restarting the runtime…")

    async def rollback(self) -> None:
        if not require_browser_control(ui_state.active_client_id):
            return
        restored = rollback_config()
        if restored is None:
            self._say("No previous config to roll back to")
            return
        await self._restart(f"Restored {restored}; restarting the runtime…")

    async def _restart(self, message: str) -> None:
        self._say(message)
        if restart_runtime is None:
            self._say(message + " (restart Commander to load it)")
            return
        try:
            await restart_runtime()
        except Exception as exc:
            logger.exception("runtime restart failed")
            self._say(f"Runtime restart failed: {type(exc).__name__}: {exc}")
            return
        self._say(
            "Runtime restarted on the installed config; home the arm before calibrating again"
        )
