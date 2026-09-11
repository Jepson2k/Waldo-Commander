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
from importlib.metadata import entry_points

import pytest
from nicegui.testing import User
from waldoctl.discovery import available_backends

from tests.helpers.wait import poll_until, wait_for_app_ready, wait_until


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
async def test_commander_runs_on_the_par6_runtime(
    par6_env: None, user: User, monkeypatch
) -> None:
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

        from waldoctl.signals import DigitalSignal
        from waldo_commander.skills.signals import (
            read_signal,
            wait_signal,
            write_signal,
        )

        signal = DigitalSignal(
            "par6", "output", 0, robot.digital_inputs, robot.digital_outputs
        )
        client = waldoctl.commander.client
        before_io = await client.io(timeout=2)
        assert before_io is not None
        try:
            observation = await write_signal.async_call(client, signal, True)
            assert observation.value and observation.source == "controller"
            assert (await read_signal.async_call(client, signal)).value
            result = await wait_signal.async_call(client, signal, False, timeout=0.2)
            assert (
                result.outcome == "timeout"
                and result.observation is not None
                and result.observation.value
            )
            user.find(marker="tab-setup").click()
            from nicegui import ui

            user.find(kind=ui.tab, content="Signals").click()

            def element(marker):
                return next(iter(user.find(marker=marker).elements))

            element("signal-direction").set_value("output")
            user.find(marker="signal-read").click()
            await user.should_see("Observed logical value: True", retries=30)
            user.find(marker="signal-write").click()
            await user.should_see(
                "Controller reports logical output: False", retries=30
            )
            assert (await client.io(timeout=2))[robot.digital_inputs] == 0
        finally:
            await client.write_io(0, before_io[robot.digital_inputs], timeout=2)

        # UI actually rendered against this backend.
        await user.should_see(marker="btn-estop")
        await user.should_see(marker="readout-x")

        # v0.8.0 surface, live off the wire: the runtime's own mode name
        # lands on commander.status for API consumers.
        import asyncio

        await wait_until(lambda: bool(status.controller.mode))
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

        # The physics pass refines the plan the editor adopted: a planned
        # program yields a tick record, not a permanently pending scrub bar.
        from waldo_commander.services.path_visualizer import path_visualizer

        program = waldoctl.commander.programs.active
        assert program is not None
        assert robot.has_physics_simulation
        target = [float(v) for v in status.joints.angles.deg]
        target[0] += 5.0
        source = (
            "from par6 import RobotClient\n"
            "with RobotClient() as rbt:\n"
            "    rbt.home()\n"
            f"    rbt.move_j({target!r}, speed=0.5)\n"
        )
        assert (
            await path_visualizer.update_path_visualization(source, program.id) is None
        )
        assert program.dry_run.path_segments, "the planning pass produced no path"
        assert program.dry_run.ticks_pending
        assert await path_visualizer.update_physics_simulation(program.id) is None
        assert program.dry_run.ticks is not None, "the physics pass never ran"
        assert not program.dry_run.ticks_pending

        # Diagnostics off the wire, all of it from the status broadcast:
        # the loop's tail, the drives' readings, and the torque series the
        # chart draws.
        user.find(marker="tab-diagnostics").click()
        await asyncio.sleep(0)
        await user.should_see(marker="diagnostics-panel")
        # A section reveals on the first status tick that finds it reportable,
        # and only while the tab is open — so wait for the reveal, not the
        # panel.
        await user.should_see(marker="diag-section-drives", retries=50)
        await user.should_see(marker="diag-section-loop", retries=50)

        def _text(marker: str) -> str:
            return next(iter(user.find(marker=marker).elements)).text

        temps = await poll_until(
            lambda: [_text(f"diag-drive-temp-{j}") for j in range(1, 7)],
            lambda t: all(v != "—" for v in t),
            timeout_s=10.0,
            what=lambda: (
                f"drive temperatures on STATUS (note: {_text('diag-drives-note')!r})"
            ),
        )
        assert all(float(t) > 0 for t in temps), f"drive temperatures read {temps}"
        assert status.drive_health.bus_voltage_v is not None
        assert _text("diag-drive-supply").endswith(" V")
        # The tool drive answers a temperature but no current, and an
        # unanswered register must read as unknown rather than as zero.
        assert _text("diag-drive-current-7") == "—"

        await poll_until(
            lambda: _text("diag-loop-p99"),
            lambda t: "budget" in t,
            timeout_s=10.0,
            what="the loop tail",
        )
        assert status.loop_health.measured
        assert _text("diag-loop-rate").endswith("Hz target")
        # The chart's own feed. The page consumes the dirty flag on every
        # status tick, so ask the buffer how many samples it holds rather
        # than racing it for a dirty read.
        await poll_until(
            lambda: len(robot_state.torque_time_series),
            bool,
            timeout_s=5.0,
            interval=0.05,
            what="joint torques reaching the chart",
        )

        # Backend branches without the optional Drives plugin still exercise
        # the complete runtime/status path above. A declared but broken plugin
        # must fail the UI checks below, rather than disappear from coverage.
        if any(
            ep.name == "par6-drives" for ep in entry_points(group="waldoctl.panels")
        ):
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
            await wait_until(
                lambda: _text("drives-temp-0").endswith("°C"), timeout_s=10.0
            )
            assert _text("drives-temp-0").endswith("°C"), (
                f"drive 0 never reported a temperature: {_text('drives-temp-0')!r}"
            )

            ilim = next(iter(user.find(marker="drives-gain-ilim_ma").elements))
            await wait_until(lambda: bool(ilim.value))
            configured = float(ilim.value)
            assert configured > 0, (
                "the current limit is seeded from the runtime's config"
            )
            ilim.value = configured * 100
            user.find(marker="drives-apply-gains").click()
            await wait_until(lambda: "ceiling" in _text("drives-gain-note"))
            assert "ceiling" in _text("drives-gain-note"), (
                f"the runtime's refusal never reached the form: {_text('drives-gain-note')!r}"
            )

            # The bus table is the runtime's scan, not a static list: every
            # configured joint answers on a sim bus.
            user.find(marker="drives-rescan").click()
            table = next(iter(user.find(marker="drives-bus-table").elements))
            await wait_until(lambda: bool(table.rows))
            present = {row["node"] for row in table.rows if row["present"] == "yes"}
            assert {0, 1, 2, 3, 4, 5} <= present, f"scan rows: {table.rows}"

        import numpy as np
        from par6 import config as par6_config
        from waldo_commander.skills import gripper_open, gripper_close, retract
        from waldoctl.setup import Pose
        from par6._par6 import pose_matrix

        mixed = Pose((0, 0, 0, 37, 25, -28))
        assert mixed.matrix() == pytest.approx(
            np.asarray(
                pose_matrix([0, 0, 0], np.radians(mixed.values[3:]).tolist())
            ).reshape(4, 4)
        ), "shared setup rotation must match PAR6's native pose conversion"

        client = waldoctl.commander.client
        park = np.degrees(par6_config.config().park_pose_rad()).tolist()
        await client.reset()
        async with asyncio.timeout(20):
            while True:
                await client.teleport(park)
                if await client.wait_status(
                    lambda s: s.homed and np.allclose(s.angles, park, atol=0.5),
                    timeout=0.5,
                ):
                    break
        before = await client.pose()
        assert before is not None
        native = await client.status()
        assert native is not None
        assert Pose(tuple(before)).matrix()[:3, :3] == pytest.approx(
            np.asarray(native.pose).reshape(4, 4)[:3, :3], abs=0.01
        ), "setup pose rotations must agree with the native PAR6 transform"
        await retract.async_call(client, distance_mm=10, speed=0.2)
        after = await client.pose()
        assert after is not None
        assert np.linalg.norm(np.array(after[:3]) - before[:3]) == pytest.approx(
            10, abs=1.0
        )
        assert await client.select_tool(par6_config.fitted_tool_key()) >= 0
        index = await client.tool.calibrate()
        assert await client.wait_command(index, timeout=15)
        await gripper_close.async_call(client)
        assert await client.wait_status(
            lambda s: s.tool_status is not None
            and bool(s.tool_status.positions)
            and s.tool_status.positions[0] > 0.9,
            timeout=5,
        )
        await gripper_open.async_call(client)
        assert await client.wait_status(
            lambda s: s.tool_status is not None
            and bool(s.tool_status.positions)
            and s.tool_status.positions[0] < 0.1,
            timeout=5,
        )
        from nicegui import ui

        user.find(marker="tab-setup").click()
        user.find(kind=ui.tab, content="TCP").click()
        user.find(marker="tcp-calibration-read").click()
        await user.should_see(
            "Read the controller's applied TCP transform.", retries=50
        )
        correction = (4.0, -2.0, 20.0, 12.0, -18.0, 7.0)
        for axis, value in zip(("x", "y", "z", "roll", "pitch", "yaw"), correction):
            next(iter(user.find(marker=f"tcp-calibration-{axis}").elements)).set_value(
                value
            )
        user.find(marker="tcp-calibration-apply").click()
        await user.should_see(
            "Controller confirmed the displayed TCP transform.", retries=100
        )
        assert await client.tcp_transform() == pytest.approx(correction)
        angles = await client.angles()
        pose = await client.pose()
        assert angles is not None and pose is not None
        fk = robot.fk(np.radians(angles), np.empty(6))
        local = Pose(tuple([*(fk[:3] * 1000), *np.degrees(fk[3:])])).matrix()
        applied = Pose(tuple(pose)).matrix()
        assert local[:3, 3] == pytest.approx(applied[:3, 3], abs=0.1)
        assert local[:3, :3] == pytest.approx(applied[:3, :3], abs=0.003)

        from dataclasses import replace
        from tests.test_vision import localization_scene
        from tests.test_handeye_panel_integration import _FrameBackend, _jpeg
        from waldo_commander.services import camera_service as camera_module
        from waldo_commander.camera_sources import CommanderCameraSource
        from waldo_commander.services.camera_session import CameraSession
        from waldo_commander.services.tcp_calibration import observe_tcp
        from waldo_commander.skills import locate_board
        from waldoctl.setup import TcpCalibration

        monkeypatch.setattr(camera_module, "LinuxpyBackend", _FrameBackend)
        monkeypatch.setattr(camera_module, "OpenCVBackend", _FrameBackend)
        camera = camera_module.camera_service
        calibration, setup, image, expected = localization_scene()
        _FrameBackend.holder["jpeg"] = _jpeg(image)
        session = CameraSession(camera.next_snapshot)
        try:
            camera.start(0)
            observation = await observe_tcp(client)
            tool = TcpCalibration(
                observation.applied,
                observation.binding.tool_key,
                observation.binding.variant_key,
            )
            tcp_wrf = observation.nominal_tool.matrix() @ tool.matrix()
            calibration = replace(
                calibration,
                camera_id=camera.camera_id,
                backend="par6",
                mount="tool",
                pose=Pose.from_matrix(
                    np.linalg.inv(tcp_wrf) @ calibration.pose.matrix(), frame="TCP"
                ),
                tool=tool,
                reference_wrf=None,
            )
            env = await session.start()
            source = CommanderCameraSource(
                env["WALDO_CAMERA_ENDPOINT"], env["WALDO_CAMERA_TOKEN"]
            )
            localized = await locate_board.async_call(
                client, calibration, source, setup, timeout_s=3
            )
            assert localized.outcome == "found", localized
            assert localized.pose.matrix()[:3, 3] == pytest.approx(
                expected[:3, 3], abs=2
            )
        finally:
            await session.close()
            camera.stop()

        from waldo_commander.components.script_execution import script_exec

        user.find(marker="tab-program").click()
        await asyncio.sleep(0)
        ui_state.active_textarea.value = (
            "from par6 import RobotClient\nwith RobotClient() as rbt:\n"
            "    index = rbt.delay(60)\n    rbt.wait_command(index, timeout=90)\n"
        )
        try:
            await script_exec.start()
            program = waldoctl.commander.programs.active
            handle = script_exec.script_handle
            assert program is not None and handle is not None
            await poll_until(
                client.queue_state,
                lambda q: q is not None and q.executing_index >= 0,
                timeout_s=25,
                what=lambda: "native program start; log="
                + "\n".join(entry.text for entry in program.log.entries),
            )
            # The command lasts longer than one wait_command polling window.
            # A timeout must not emit a completed step or advance the program.
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(12):
                    while not program.dry_run.playback.executing_step_at_end:
                        await asyncio.sleep(0.05)
            assert handle["proc"].returncode is None
            await script_exec.stop()
            await poll_until(
                client.queue_state,
                lambda q: q is not None and q.executing_index < 0,
                timeout_s=3,
                what="the stopped native queue",
            )
            index = await client.delay(0.01)
            assert await client.wait_command(index, timeout=3), (
                "Stop must clear the previous program's native queue"
            )
        finally:
            if script_exec.script_handle is not None:
                await script_exec.stop()
    finally:
        # main.py never owns the spawned runtime's lifetime; the test does.
        robot = getattr(ui_state, "robot", None)
        if robot is not None:
            with contextlib.suppress(Exception):
                robot.stop()
