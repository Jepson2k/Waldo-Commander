"""Live camera IPC, generated programs, localization and bounded session lifetime."""

import asyncio
import json
import time
from dataclasses import replace

import numpy as np
import pytest
import waldoctl
from waldoctl.setup import Pose, TcpCalibration

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
)
from tests.test_handeye_panel_integration import _FrameBackend, _jpeg
from tests.test_vision import localization_scene
from waldo_commander.camera import CameraUnavailable
from waldo_commander.camera_sources import CommanderCameraSource, ImageFixture
from waldo_commander.services.camera_service import camera_service
from waldo_commander.services.camera_session import CameraSession
from waldo_commander.services.script_runner import (
    create_default_config,
    run_script,
    stop_script,
)
from waldo_commander.services.tcp_calibration import observe_tcp
from waldo_commander.setup import SetupStore
from waldo_commander.skills import locate_board


@pytest.mark.integration
async def test_camera_localization_program_preview_and_session_lifetime(
    user, tmp_path, monkeypatch
):
    from waldo_commander.services import camera_service as module
    from waldo_commander.services.path_visualizer import UNCHANGED, path_visualizer
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.state import ui_state

    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path / "setup"))
    monkeypatch.setattr(module, "LinuxpyBackend", _FrameBackend)
    monkeypatch.setattr(module, "OpenCVBackend", _FrameBackend)
    calibration, setup, image, expected = localization_scene()
    _FrameBackend.holder["jpeg"] = _jpeg(image)
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    session = CameraSession(camera_service.next_snapshot)
    handle = None
    try:
        camera_service.start(0)
        calibration = replace(calibration, camera_id=camera_service.camera_id)
        setup = setup.with_camera("overhead", calibration)
        SetupStore().save("bench", setup)
        env = await session.start()
        source = CommanderCameraSource(
            env["WALDO_CAMERA_ENDPOINT"], env["WALDO_CAMERA_TOKEN"]
        )
        first = await source.snapshot(timeout_s=3)
        second = await source.snapshot(timeout_s=3)
        assert (
            second.sequence > first.sequence
            and second.jpeg == _FrameBackend.holder["jpeg"]
        )
        assert second.received_at >= first.received_at
        with pytest.raises(CameraUnavailable, match="authorization"):
            await replace(source, token="incorrect").snapshot()
        for timeout in (0, -1, float("nan"), float("inf"), True, 6):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", int(source.endpoint.split(":")[1])
            )
            writer.write(
                json.dumps({"token": source.token, "timeout_s": timeout}).encode()
                + b"\n"
            )
            await writer.drain()
            assert "error" in json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()

        rbt = waldoctl.commander.client
        found = await locate_board.async_call(
            rbt, calibration, source, setup, timeout_s=3
        )
        assert found.outcome == "found", found
        np.testing.assert_allclose(found.pose.matrix()[:3, 3], expected[:3, 3], atol=2)
        with pytest.raises(ValueError, match="preview client"):
            await locate_board.async_call(rbt, calibration, ImageFixture(first), setup)

        robot = await observe_tcp(rbt)
        tool = TcpCalibration(
            robot.applied, robot.binding.tool_key, robot.binding.variant_key
        )
        tcp = robot.nominal_tool.matrix() @ tool.matrix()
        wrist = replace(
            calibration,
            mount="tool",
            pose=Pose.from_matrix(
                np.linalg.inv(tcp) @ calibration.pose.matrix(), frame="TCP"
            ),
            tool=tool,
            reference_wrf=None,
        )
        wrist_result = await locate_board.async_call(
            rbt, wrist, source, setup, timeout_s=3
        )
        np.testing.assert_allclose(
            wrist_result.pose.matrix(), found.pose.matrix(), atol=0.01
        )
        with pytest.raises(ValueError, match="TCP transform"):
            await locate_board.async_call(
                rbt,
                replace(wrist, tool=replace(tool, values=(1, 0, 0, 0, 0, 0))),
                source,
                setup,
            )

        def element(marker):
            return next(iter(user.find(marker=marker).elements))

        user.find(marker="tab-program").click()
        await asyncio.sleep(0)
        ui_state.active_textarea.value = (
            "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
        )
        user.find(marker="tab-skills").click()
        element("skill-choice").set_value("waldo.locate_board")
        user.find(marker="skill-insert").click()
        await user.should_see("Inserted Python skill call")
        program = waldoctl.commander.programs.active
        assert "CameraCalibration.from_dict(" in program.source
        assert "CommanderCameraSource()" in program.source
        assert source.token not in program.source
        error = await path_visualizer.update_path_visualization(
            program.source, tab_id=program.id
        )
        assert error is not None and "ImageFixture" in error
        fixture_path = tmp_path / "board.jpg"
        fixture_path.write_bytes(first.jpeg)
        preview_source = (
            "from waldo_commander.camera_sources import ImageFixture\n"
            + program.source.replace(
                "CommanderCameraSource()",
                f"ImageFixture.from_file({str(fixture_path)!r}, camera_id={calibration.camera_id!r})",
            )
        )
        assert await path_visualizer.update_path_visualization(
            preview_source, tab_id=program.id
        ) in (None, UNCHANGED)
        user.find(marker="skill-run").click()
        await user.should_see("Skill completed", retries=300)
        assert script_exec.last_exit_code == 0

        script = tmp_path / "camera_program.py"
        script.write_text(
            "import asyncio\nfrom waldo_commander.camera_sources import CommanderCameraSource\nasync def main():\n    source = CommanderCameraSource()\n    first = await source.snapshot(timeout_s=3)\n    second = await source.snapshot(timeout_s=3)\n    assert second.sequence > first.sequence\n    print('fresh frames received', flush=True)\nasyncio.run(main())\n"
        )
        stdout, stderr = [], []
        handle = await run_script(
            create_default_config(str(script)), stdout.append, stderr.append
        )
        child_session = handle["camera_session"]
        port = child_session.server.sockets[0].getsockname()[1]
        async with asyncio.timeout(20):
            await handle["proc"].wait()
            await asyncio.gather(
                handle["stdout_task"], handle["stderr_task"], handle["camera_cleanup"]
            )
        assert handle["proc"].returncode == 0 and stdout == ["fresh frames received"], (
            stderr
        )
        with pytest.raises(CameraUnavailable):
            await CommanderCameraSource(
                f"127.0.0.1:{port}", child_session.token
            ).snapshot()
        script.write_text("import time\nprint('waiting', flush=True)\ntime.sleep(60)\n")
        stdout.clear()
        handle = await run_script(
            create_default_config(str(script)), stdout.append, stderr.append
        )
        async with asyncio.timeout(15):
            while not stdout:
                await asyncio.sleep(0.02)
        await stop_script(handle)
        assert handle["camera_session"].closed and not handle["camera_session"].tasks

        _FrameBackend.holder["jpeg"] = _jpeg(np.full_like(image, 255))
        missing = await locate_board.async_call(rbt, calibration, source, setup)
        assert missing.outcome == "missing" and missing.pose is None
        _FrameBackend.holder["jpeg"] = b""
        start = time.monotonic()
        with pytest.raises(CameraUnavailable, match="deadline"):
            await source.snapshot(timeout_s=0.1)
        assert time.monotonic() - start < 1
        waiting = asyncio.create_task(source.snapshot(timeout_s=5))
        async with asyncio.timeout(3):
            while not session.tasks:
                await asyncio.sleep(0.01)
        await session.close()
        with pytest.raises(CameraUnavailable):
            await waiting
        assert not session.tasks
    finally:
        if handle is not None:
            await stop_script(handle)
        if script_exec.script_handle is not None:
            await script_exec.stop()
        await session.close()
        camera_service.stop()
