"""Portable project selection and validation through real files and robot clients."""

import hashlib
import io
import json
import stat
import zipfile
import asyncio
import math
from dataclasses import asdict

import pytest
from nicegui.testing import User
import waldoctl
from waldoctl.setup import SetupSnapshot, Parameter, TcpCalibration
from waldoctl.shapes import ShapeWorld, Sphere
from waldoctl.world import world_to_dict

from waldo_commander.services.portable_projects import (
    MANIFEST,
    export_project,
    inspect_project,
    import_project,
    dependency_report,
)


def test_export_reserves_space_for_its_manifest(monkeypatch):
    from waldo_commander.services import portable_projects

    monkeypatch.setattr(portable_projects, "MAX_ARCHIVE_BYTES", 4096)
    with pytest.raises(ValueError, match="unpacked size"):
        export_project({"programs/main.py": b"#" * 4096})


def test_dependency_report_does_not_claim_extras_are_verified():
    assert any(
        "extras" in issue.lower()
        for issue in dependency_report(["waldoctl[unavailable_extra]"])
    )


def test_selected_archive_round_trip_rejects_unsafe_files_before_writing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ACCOUNT_TOKEN", "unselected-environment-secret")
    (tmp_path / "private.txt").write_text("unselected-file-secret")
    source = "raise RuntimeError('Import must not execute this program')\n"
    snapshot = SetupSnapshot(parameters={"offset": Parameter(3, "mm")})
    files = {
        "programs/main.py": source.encode(),
        "programs/helpers.py": b"def offset(): return 3\n",
        "setups/bench.json": json.dumps(snapshot.to_dict()).encode(),
    }
    requirements = ["waldoctl>=999999", "missing-waldo-project-test-dependency==1"]
    data = export_project(files, requirements=requirements)
    manifest, unpacked = inspect_project(data)
    assert unpacked == files
    assert len(dependency_report(manifest["requirements"])) == 2
    destination = import_project(data, tmp_path / "imports", name="bench")
    assert (destination / "programs/main.py").read_text() == source
    again = import_project(data, tmp_path / "imports", name="bench")
    assert destination != again
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        contents = b"".join(archive.read(name) for name in archive.namelist())
    assert b"unselected-environment-secret" not in contents
    assert b"unselected-file-secret" not in contents
    original = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }

    def archive_with(path, content, *, mode=None, checksum=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            entry = zipfile.ZipInfo(path)
            if mode is not None:
                entry.create_system = 3
                entry.external_attr = mode << 16
            archive.writestr(entry, content)
            archive.writestr(
                MANIFEST,
                json.dumps(
                    {
                        "schema": 1,
                        "requirements": [],
                        "files": [
                            {
                                "path": path,
                                "size": len(content),
                                "sha256": checksum
                                or hashlib.sha256(content).hexdigest(),
                            }
                        ],
                    }
                ),
            )
        return output.getvalue()

    invalid = [
        archive_with(path, b"pass")
        for path in (
            "../outside.py",
            "/tmp/outside.py",
            "programs/../../outside.py",
            "programs\\..\\outside.py",
            "programs/C:outside.py",
            "programs/CON.py",
            "programs/.env",
            "programs/extra.txt",
        )
    ]
    invalid += [
        archive_with("programs/link.py", b"../../outside", mode=stat.S_IFLNK | 0o777),
        archive_with("programs/main.py", b"pass", checksum="0" * 64),
        archive_with("setups/bench.json", b'{"version": 1}'),
        b"not a ZIP file",
    ]
    for broken in invalid:
        with pytest.raises(ValueError):
            import_project(broken, tmp_path / "refused")
        assert not (tmp_path / "refused").exists()
    assert original == {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    with pytest.raises(ValueError, match="case-insensitive"):
        export_project({"programs/Main.py": b"pass", "programs/main.py": b"pass"})


@pytest.mark.integration
async def test_imported_program_uses_its_own_data_in_preview_execution_and_save(
    user: User, tmp_path, monkeypatch
):
    from tests.helpers.wait import (
        enable_sim,
        ensure_robot_ready_for_motion,
        wait_for_app_ready,
    )
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.path_visualizer import path_visualizer
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.setup import SetupStore
    from waldo_commander.demonstrations import record_demonstration, load_demonstration
    from waldo_commander.state import ui_state

    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path / "global-setups"))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    before = await client.angles()
    tcp = await client.tcp_transform()
    world = await client.shapes()
    # This archive needs two observed samples, independent of runner cadence.
    recording = await record_demonstration(client, duration_s=5, max_samples=2)
    assert recording.ended == "sample_limit"
    assert len(recording.samples) == 2
    SetupStore().save(
        "bench", SetupSnapshot(parameters={"j1": Parameter(before[0] - 4, "deg")})
    )
    snapshot = SetupSnapshot(
        parameters={"j1": Parameter(before[0] + 2, "deg")},
        tcp_calibrations={"tip": TcpCalibration((2, 3, 4, 5, 6, 7), "NONE")},
    )
    source = """import json
from parol6 import RobotClient
from helpers import destination
from waldo_commander.project import project_file
from waldo_commander.setup import load_setup
from waldo_commander.demonstrations import load_demonstration
from waldoctl.world import world_from_dict
setup = load_setup("bench")
observations = load_demonstration(project_file("recordings/capture.json"))
assert len(observations.samples) >= 2
world = world_from_dict(json.loads(project_file("worlds/fixture.json").read_text()))
assert len(world.program) == 1
with RobotClient() as rbt:
    rbt.move_j(destination(setup), duration=0.5)
"""
    helper = f"def destination(setup):\n    return [setup.parameters['j1'].value, {', '.join(repr(v) for v in before[1:])}]\n"
    files = {
        "programs/main.py": source.encode(),
        "programs/helpers.py": helper.encode(),
        "setups/bench.json": json.dumps(snapshot.to_dict()).encode(),
        "recordings/capture.json": json.dumps(
            {"schema": 1, **asdict(recording)}
        ).encode(),
        "worlds/fixture.json": json.dumps(
            world_to_dict(
                ShapeWorld(
                    program=(
                        Sphere(name="fixture", radius=0.01, pose=(2, 2, 2, 0, 0, 0)),
                    )
                )
            )
        ).encode(),
    }
    data = export_project(files, requirements=["waldoctl>=0.2"])
    editor = ui_state.editor_panel
    assert editor is not None
    editor.PROGRAM_DIR = tmp_path / "programs"
    script_exec.set_program_dir(editor.PROGRAM_DIR)
    destination_path = import_project(
        data, editor.PROGRAM_DIR / "projects", name="bench"
    )
    assert not is_any_program_running()
    assert await client.angles() == pytest.approx(before, abs=0.05)
    assert await client.tcp_transform() == tcp
    assert await client.shapes() == world
    assert SetupStore().load("bench").parameters["j1"].value == before[0] - 4
    assert load_demonstration(destination_path / "recordings/capture.json") == recording
    assert SetupStore(destination_path / "setups").load("bench").tcp_calibrations[
        "tip"
    ].values == (2, 3, 4, 5, 6, 7)

    with user.client:
        await editor.load_program(str(destination_path / "programs/main.py"))
    program = waldoctl.commander.programs.active
    assert program is not None
    error = await path_visualizer.update_path_visualization(
        program.source, tab_id=program.id
    )
    assert error is None, error
    assert program.dry_run.path_segments
    last = program.dry_run.path_segments[-1]
    assert last.is_valid and last.joints is not None
    assert math.degrees(last.joints[0]) == pytest.approx(before[0] + 2, abs=0.05)
    assert await client.angles() == pytest.approx(before, abs=0.05)

    # Preview workers are reused: an edited helper must reach the next
    # preview instead of the module cached by the previous one.
    import numpy as np
    from parol6.client.dry_run_client import DryRunRobotClient

    from waldo_commander.services.path_visualizer import (
        PathSegment,
        _run_simulation_isolated,
    )

    def preview_in_process():
        return _run_simulation_isolated(
            program.source,
            np.radians(before),
            dry_run_client_cls=DryRunRobotClient,
            setup_directory=str(destination_path / "setups"),
            program_path=str(destination_path / "programs/main.py"),
        )

    first = preview_in_process()
    assert first["error"] is None, first["error"]
    (destination_path / "programs/helpers.py").write_text(
        helper.replace(
            "setup.parameters['j1'].value", "setup.parameters['j1'].value + 3"
        )
    )
    second = preview_in_process()
    assert second["error"] is None, second["error"]
    refreshed = PathSegment.from_dict(second["segments"][-1])
    assert refreshed.joints is not None
    assert math.degrees(refreshed.joints[0]) == pytest.approx(
        before[0] + 5, abs=0.05
    ), "the preview ran the helper cached by the previous preview"
    (destination_path / "programs/helpers.py").write_text(helper)
    assert await script_exec.start()
    async with asyncio.timeout(20):
        while is_any_program_running():
            await asyncio.sleep(0.05)
    assert script_exec.last_exit_code == 0, "\n".join(
        e.text for e in program.log.entries
    )
    assert (await client.angles())[0] == pytest.approx(before[0] + 2, abs=0.05)
    assert await client.tcp_transform() == tcp
    assert await client.shapes() == world

    assert ui_state.active_textarea is not None
    ui_state.active_textarea.value = source + "\n# Project edit\n"
    await asyncio.sleep(0)
    await editor.save_program()
    assert (
        (destination_path / "programs/main.py").read_text().endswith("# Project edit\n")
    )
    assert program.file_path == str(destination_path / "programs/main.py")
