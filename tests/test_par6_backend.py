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

        # v0.8.0 surface, live off the wire: the controller chip carries
        # the runtime's own mode name.
        import asyncio

        for _ in range(50):
            if status.controller.mode:
                break
            await asyncio.sleep(0.1)
        assert status.controller.mode, "no controller mode ever arrived"
        await user.should_see(marker="controller-chip")
    finally:
        # main.py never owns the spawned runtime's lifetime; the test does.
        robot = getattr(ui_state, "robot", None)
        if robot is not None:
            with contextlib.suppress(Exception):
                robot.stop()
