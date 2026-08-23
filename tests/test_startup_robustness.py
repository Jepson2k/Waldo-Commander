"""Startup robustness: persisted sim/hardware mode and robot-less boot.

Covers GitHub issues #7 (page never renders in hardware mode when no robot
is wired) and #8 (sim/hardware mode not persisted across restarts).
"""

import asyncio

import pytest
from nicegui import app as ng_app
from nicegui.testing import User

import waldoctl

from tests.helpers.wait import wait_for_app_ready, wait_for_urdf_ready
from waldo_commander.state import ui_state


async def _wait_for_stored_mode(expected: str, timeout_s: float = 2.5) -> None:
    interval = 0.05
    for _ in range(int(timeout_s / interval)):
        if ng_app.storage.general.get("startup_mode") == expected:
            return
        await asyncio.sleep(interval)
    raise TimeoutError(
        f"startup_mode did not become {expected!r}, "
        f"still {ng_app.storage.general.get('startup_mode')!r}"
    )


@pytest.mark.integration
async def test_sim_toggle_persists_startup_mode(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The robot/sim toggle writes the ``startup_mode`` preference.

    The backend flip is stubbed: actually leaving simulator mode makes the
    controller open the real serial port, which doesn't exist on a test box
    (same approach as test_set_simulator_syncs_gui_mode_visuals). The subject
    is the persisted preference and the GUI-side mode flag.
    """
    await user.open("/")
    await wait_for_app_ready()

    panel = ui_state.control_panel
    assert waldoctl.commander.status.simulator_active is True

    async def _fake_simulator(enabled: bool) -> int:
        return 1

    monkeypatch.setattr(panel.client, "simulator", _fake_simulator)
    try:
        user.find(marker="btn-robot-toggle").click()
        await _wait_for_stored_mode("hardware")
        assert waldoctl.commander.status.simulator_active is False

        user.find(marker="btn-robot-toggle").click()
        await _wait_for_stored_mode("sim")
        assert waldoctl.commander.status.simulator_active is True
    finally:
        waldoctl.commander.status.simulator_active = True
        ng_app.storage.general.pop("startup_mode", None)


@pytest.mark.unit
async def test_set_initial_mode_honors_stored_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted ``startup_mode`` wins over port-based mode detection.

    ``_set_initial_mode`` runs at app startup, before the user fixture can
    seed storage, so it is exercised directly against the live controller
    (same approach as test_control_mode_persists_across_restart).
    """
    from parol6 import AsyncRobotClient

    import waldo_commander.main as wc_main
    from tests.conftest import CONTROLLER_PORT

    status = waldoctl.commander.status
    async with AsyncRobotClient(
        host="127.0.0.1", port=CONTROLLER_PORT, timeout=5.0
    ) as client:
        # main() has not run in this module instance, so bind a real client.
        monkeypatch.setattr(wc_main, "client", client, raising=False)
        try:
            # "hardware" with no port would boot with no transport at all (a
            # dead app) — the no-port invariant wins and forces simulator.
            ng_app.storage.general["startup_mode"] = "hardware"
            status.simulator_active = False
            await wc_main._set_initial_mode("")
            assert status.simulator_active is True

            # "hardware" with a port: the real transport is kept.
            ng_app.storage.general["startup_mode"] = "hardware"
            status.simulator_active = False
            await wc_main._set_initial_mode("/dev/ttyACM0")
            assert status.simulator_active is False

            # "sim" with a port: port-based detection would keep the transport.
            ng_app.storage.general["startup_mode"] = "sim"
            status.simulator_active = False
            await wc_main._set_initial_mode("/dev/ttyACM0")
            assert status.simulator_active is True

            # Unset: no port means simulator.
            ng_app.storage.general.pop("startup_mode", None)
            status.simulator_active = False
            await wc_main._set_initial_mode("")
            assert status.simulator_active is True
        finally:
            ng_app.storage.general.pop("startup_mode", None)
            status.simulator_active = True
            await client.simulator(True)


@pytest.mark.integration
async def test_page_renders_without_backend_status(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no STATUS frame the page must still render, not block forever.

    A boot where no STATUS frame ever arrives means ``app_ready`` never
    fires. The page-init fall-through must build the URDF scene anyway and
    show the degraded-boot status line. (A robot-less boot lands in
    simulator mode — the no-port invariant — so no hardware banner is
    expected here.)
    """
    from waldo_commander.state import readiness_state

    # The session controller streams STATUS frames, so backend readiness was
    # marked during app startup — unmark it and keep further frames from
    # re-marking, mimicking a boot where no frame ever arrives.
    monkeypatch.setattr(readiness_state, "mark_backend_done", lambda: None)
    monkeypatch.setattr(readiness_state, "_backend_done", False)

    await user.open("/")
    # Covers the 3 s app_ready wait plus the scene build on slow runners.
    await wait_for_urdf_ready(timeout_s=20.0)
    assert not readiness_state.app_ready.is_set()

    # Degraded status line replaces the red full-page blocker.
    await user.should_see("Robot disconnected — proceeding without live data")


@pytest.mark.integration
async def test_hardware_autodetect_retries_after_slow_boot(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sim→robot auto-switch lives in check_ping and retries, so
    hardware that produces its first frames after the page-load window
    still flips the mode; a pinned sim preference suppresses the switch.

    The running app's module globals live in a runpy namespace the test
    can't reach, so the fakes go on the shared client instance and the
    pinned-sim leg runs first — the one-shot switch latch would otherwise
    confound the negative assertion.
    """

    class _HwPing:
        hardware_connected = True

    async def fake_ping():
        return _HwPing()

    async def fake_simulator(enabled: bool) -> int:
        return 1

    await user.open("/")
    await wait_for_app_ready()
    assert waldoctl.commander.status.simulator_active is True

    # Hardware "appears" only after the page finished loading.
    client = ui_state.control_panel.client
    monkeypatch.setattr(client, "ping", fake_ping)
    monkeypatch.setattr(client, "simulator", fake_simulator)
    try:
        # Pinned sim preference: no auto-switch even with hardware present.
        ng_app.storage.general["startup_mode"] = "sim"
        await asyncio.sleep(2.5)  # negative window: > 2 ping ticks at 1 Hz
        assert waldoctl.commander.status.simulator_active is True, (
            "a pinned sim preference must suppress the auto-switch"
        )

        # Unpinned: the retrying detect flips out of simulator.
        ng_app.storage.general.pop("startup_mode", None)
        for _ in range(60):  # check_ping fires at 1 Hz
            if waldoctl.commander.status.simulator_active is False:
                break
            await asyncio.sleep(0.1)
        assert waldoctl.commander.status.simulator_active is False, (
            "late hardware must still flip out of simulator"
        )
    finally:
        ng_app.storage.general.pop("startup_mode", None)
        waldoctl.commander.status.simulator_active = True
