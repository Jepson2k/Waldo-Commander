"""Waldo Commander driving the par6 backend against a live `par6d --sim`.

The rest of the suite runs on parol6. This one boots the app with
``WALDO_ROBOT=par6`` so the whole stack — backend discovery, robot start,
async client, status pipeline, UI — is exercised against the Rust runtime
over protocol v2, not a mock.

Must run in its own pytest process, so it is gated behind
``WALDO_PAR6_E2E=1`` and skipped in the ordinary suite:

    WALDO_PAR6_E2E=1 PAR6D_BIN=/path/to/par6d pytest tests/test_par6_backend.py

Backend choice, controller port and exclusive-start are process-global, and
NiceGUI's `user` fixture imports the app module once per session against the
parol6 controller the session fixtures start. Sharing a process with that
setup leaves par6d running but its STATUS frames never reaching the app's
consumer, so this asserts nothing useful there.

The opt-in is also the honesty boundary: without ``WALDO_PAR6_E2E`` the file
skips, so an ordinary checkout runs green — but once it is set, a missing
par6 backend or ``par6d`` binary is a failure, not a skip. CI sets the flag
precisely to run this test; letting it skip there would be a silent green.
"""

import contextlib
import os
import shutil
import socket

import pytest
from nicegui.testing import User
from waldoctl.discovery import available_backends

from tests.helpers.wait import wait_for_app_ready


def _par6d_binary() -> str | None:
    """Resolve the par6d binary the same way par6's Robot does."""
    env_bin = os.environ.get("PAR6D_BIN")
    if env_bin:
        return env_bin if os.path.isfile(env_bin) else None
    return shutil.which("par6d")


requires_par6 = pytest.mark.skipif(
    not os.environ.get("WALDO_PAR6_E2E"),
    reason="needs WALDO_PAR6_E2E=1 — the par6 e2e must run in its own pytest process",
)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def par6_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the app at par6 before the `user` fixture imports main.py.

    main() runs at import, so the backend is chosen during the `user`
    fixture's setup — env set inside the test body would be too late. Listing
    this fixture ahead of `user` in the signature is what orders it first.
    """
    if "par6" not in available_backends():
        pytest.fail(
            "WALDO_PAR6_E2E=1 but the par6 backend is not installed "
            "(pip install '.[par6]')"
        )
    if _par6d_binary() is None:
        pytest.fail(
            "WALDO_PAR6_E2E=1 but no par6d binary — set PAR6D_BIN or put "
            "par6d on PATH (cargo build -p par6d --release)"
        )
    port = _free_udp_port()
    monkeypatch.setenv("WALDO_ROBOT", "par6")
    monkeypatch.setenv("WALDO_CONTROLLER_PORT", str(port))
    # par6's Robot reads its own port var when constructed without kwargs.
    monkeypatch.setenv("PAR6_COMMAND_PORT", str(port))
    # The suite's default (0) makes the app attach to the session's parol6
    # controller and refuse to start anything; 1 is what drives the app
    # through Robot.start(), which is the spawn path under test.
    monkeypatch.setenv("WALDO_EXCLUSIVE_START", "1")


@requires_par6
@pytest.mark.integration
async def test_commander_runs_on_the_par6_runtime(par6_env: None, user: User) -> None:
    """The app boots on par6 and its status pipeline carries live runtime data.

    ``start_controller`` finds nothing at the target port, so par6's Robot
    spawns ``par6d --sim`` itself — the same reachable-or-spawn path a
    developer gets. Everything asserted below therefore came off the wire
    from a real runtime process.
    """
    from waldo_commander.state import readiness_state, robot_state, ui_state

    try:
        await user.open("/")
        await wait_for_app_ready(timeout_s=60.0)

        robot = ui_state.robot
        assert type(robot).__module__.startswith("par6"), (
            f"expected the par6 backend, got {type(robot).__module__}"
        )

        # The status consumer only flips this after a STATUS frame decodes,
        # so it is evidence of live traffic rather than of app startup.
        assert readiness_state._backend_done, "no STATUS update ever arrived from par6d"

        import waldoctl

        status = waldoctl.commander.status
        assert status.simulator_active, "par6d --sim should report simulator_active"
        assert len(status.joints.angles.deg) == robot.joints.count == 6

        # The app sizes its IO buffer from the backend's pin counts and then
        # writes decoded frames straight in, so agreement here is what keeps
        # the status pipeline from throwing on every frame.
        assert len(robot_state.io) == robot.digital_inputs + robot.digital_outputs + 1

        # UI actually rendered against this backend.
        await user.should_see(marker="btn-estop")
        await user.should_see(marker="readout-x")

        # v0.8.0 surface, live off the wire: the runtime's own mode name
        # lands on commander.status for API consumers.
        import asyncio

        for _ in range(50):
            if status.controller.mode:
                break
            await asyncio.sleep(0.1)
        assert status.controller.mode, "no controller mode ever arrived"

        # Freedrive reports the arm, not the request. A fresh `par6d --sim`
        # is unreferenced, so the runtime cannot actually release the arm
        # however willingly it takes the command — and the surface has to
        # keep saying so, or the UI tells an operator an arm is safe to
        # grab while a hold term is still on the joints.
        assert not robot_state.homed, "a fresh sim should not claim a home reference"
        assert not status.controller.freedrive

        user.find(marker="btn-freedrive").click()
        await asyncio.sleep(1.5)
        assert not status.controller.freedrive, (
            "an unreferenced arm reported itself back-driveable"
        )

        # Diagnostics off the wire, all of it from the status broadcast:
        # the loop's tail, the drives' readings, and the torque series the
        # chart draws.
        user.find(marker="tab-diagnostics").click()
        await asyncio.sleep(0)
        await user.should_see(marker="diagnostics-panel")

        def _text(marker: str) -> str:
            return next(iter(user.find(marker=marker).elements)).text

        temps: list[str] = []
        for _ in range(100):
            temps = [_text(f"diag-drive-temp-{j}") for j in range(1, 7)]
            if all(t != "—" for t in temps):
                break
            await asyncio.sleep(0.1)
        assert all(float(t) > 0 for t in temps), (
            f"drive temperatures never arrived on STATUS: {temps}; "
            f"note: {_text('diag-drives-note')!r}"
        )
        assert status.drive_health.bus_voltage_v is not None
        assert _text("diag-drive-supply").endswith(" V")
        # The tool drive answers a temperature but no current, and an
        # unanswered register must read as unknown rather than as zero.
        assert _text("diag-drive-current-7") == "—"

        for _ in range(100):
            if "budget" in _text("diag-loop-p99"):
                break
            await asyncio.sleep(0.1)
        assert status.loop_health.measured
        assert "budget" in _text("diag-loop-p99"), (
            f"loop tail never shown: {_text('diag-loop-p99')!r}"
        )
        assert _text("diag-loop-rate").endswith("Hz target")
        # The chart's own feed. The page consumes the dirty flag on every
        # status tick, so ask the buffer how many samples it holds rather
        # than racing it for a dirty read.
        for _ in range(100):
            if len(robot_state.torque_time_series):
                break
            await asyncio.sleep(0.05)
        assert len(robot_state.torque_time_series), (
            "no joint torques ever reached the chart"
        )

        # par6's own Drives tab, mounted through the generic plugin path and
        # admitted by its applies_to(). Its readings are the same STATUS the
        # Diagnostics tab reads, keyed by the config's node ids; its tuning
        # form is seeded from the runtime's stored config, and a write the
        # runtime refuses is shown on the form rather than swallowed — that
        # refusal is the ceiling a bench tool cannot enforce.
        await user.should_see(marker="tab-par6-drives")
        user.find(marker="tab-par6-drives").click()
        await asyncio.sleep(0)
        await user.should_see(marker="drives-readings")
        for _ in range(100):
            if _text("drives-temp-0").endswith("°C"):
                break
            await asyncio.sleep(0.1)
        assert _text("drives-temp-0").endswith("°C"), (
            f"drive 0 never reported a temperature: {_text('drives-temp-0')!r}"
        )

        ilim = next(iter(user.find(marker="drives-gain-ilim_ma").elements))
        for _ in range(50):
            if ilim.value:
                break
            await asyncio.sleep(0.1)
        configured = float(ilim.value)
        assert configured > 0, "the current limit is seeded from the runtime's config"
        ilim.value = configured * 100
        user.find(marker="drives-apply-gains").click()
        for _ in range(50):
            if "ceiling" in _text("drives-gain-note"):
                break
            await asyncio.sleep(0.1)
        assert "ceiling" in _text("drives-gain-note"), (
            f"the runtime's refusal never reached the form: {_text('drives-gain-note')!r}"
        )

        # The bus table is the runtime's scan, not a static list: every
        # configured joint answers on a sim bus.
        user.find(marker="drives-rescan").click()
        table = next(iter(user.find(marker="drives-bus-table").elements))
        for _ in range(50):
            if table.rows:
                break
            await asyncio.sleep(0.1)
        present = {row["node"] for row in table.rows if row["present"] == "yes"}
        assert {0, 1, 2, 3, 4, 5} <= present, f"scan rows: {table.rows}"
    finally:
        # main.py never owns the spawned runtime's lifetime; the test does.
        robot = getattr(ui_state, "robot", None)
        if robot is not None:
            with contextlib.suppress(Exception):
                robot.stop()
