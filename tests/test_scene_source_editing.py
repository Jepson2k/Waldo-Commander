"""Scene target edits preserve the authored Python around the target."""

import ast
import math

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.scene_events import gizmo, held_until, right_click, translated_ball
from tests.helpers.wait import wait_until
from tests.test_scene_jog_handles import _open
from waldo_commander.components.simulation_engine import simulation
from waldo_commander.state import ui_state


@pytest.mark.integration
async def test_target_storage_frame_rewrite_edit_undo_and_setup_relocation(
    user: User, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    from waldoctl.setup import Frame, Pose, SetupSnapshot

    from waldo_commander.setup import SetupStore

    setup = SetupSnapshot(frames={"fixture": Frame((100, 200, 30, 15, 20, 35))})
    store = SetupStore(tmp_path)
    store.save("bench", setup)
    urdf = await _open(user)
    textarea = ui_state.active_textarea
    program = waldoctl.commander.programs.active
    world = Pose((0, 280, 250, 90, 0, 90))
    source = (
        "from waldo_commander.setup import load_setup\n"
        f"cell = load_setup('bench', directory={str(tmp_path)!r})\n"
        "from parol6 import RobotClient\nrbt = RobotClient()\n"
        "rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5)\n"
        "rbt.move_l(pose=[0, 280, 250, 90, 0, 90], speed=0.5)  # approach\n"
    )
    textarea.value = source

    async def preview():
        await simulation.run_simulation()
        assert await wait_until(
            lambda: program.dry_run.commanded_revision == program.dry_run.revision, 5
        )
        assert program.dry_run.commanded.stop == "completed"
        motions = ast.parse(textarea.value).body[-2:]
        assert [t.line_number for t in program.dry_run.targets] == [
            motion.lineno for motion in motions
        ]
        return program.dry_run.targets[-1]

    async def edit():
        target = await preview()
        right_click(user, urdf.scene, [f"target:{target.id}"])
        user.find(marker="scene-edit-target").click()
        assert urdf._editing_unified_target
        assert urdf._edit_line == target.line_number

    def storage_checked(frame):
        right_click(user, urdf.scene, ["gizmo:tcp"])
        for key in ("world", "frame:fixture"):
            entry = user.find(marker=f"scene-target-store-{key}").elements.pop()
            assert entry._props["aria-checked"] == str(key == frame).lower()
            assert entry._props["role"] == "menuitemradio"

    def coordinates():
        call = ast.parse(textarea.value).body[-1].value
        argument = next(k.value for k in call.keywords if k.arg == "pose")
        if isinstance(argument, ast.List):
            return ast.literal_eval(argument)
        resolve = argument.func.value
        assert resolve.func.value.id == "cell"
        pose = resolve.args[0]
        assert pose.func.id == "Pose" and ast.literal_eval(pose.args[1]) == "fixture"
        return ast.literal_eval(pose.args[0])

    await edit()
    marker = urdf._edit_marker
    position = (marker.x, marker.y, marker.z)
    shown = urdf._requested_edit_pose
    right_click(user, urdf.scene, ["gizmo:tcp"])
    user.find(marker="scene-target-store-frame:fixture").click()
    stored = textarea.value
    expected = setup.relative_pose(world, "fixture").as_list()
    assert coordinates() == pytest.approx(expected, abs=0.00051)
    assert stored.count("from waldoctl.setup import Pose") == 1
    assert stored.startswith(
        "from waldoctl.setup import Pose\nfrom waldo_commander.setup"
    )
    assert stored.endswith("speed=0.5)  # approach\n")
    assert (marker.x, marker.y, marker.z) == pytest.approx(position)
    assert urdf._requested_edit_pose == shown
    assert urdf.scene.interaction["alignment"]["key"] == "frame:fixture"
    urdf.cancel_target_edit()
    await edit()
    assert urdf.scene.interaction["alignment"]["key"] == "frame:fixture"
    storage_checked("frame:fixture")
    user.find(marker="scene-gizmo-align-tool").click()
    user.find(marker="object-undo").click()
    assert textarea.value == source
    user.find(marker="object-redo").click()
    assert textarea.value == stored
    await edit()
    assert urdf.scene.interaction["alignment"]["key"] == "tool"
    storage_checked("frame:fixture")
    user.find(marker="scene-gizmo-align-frame:fixture").click()
    # Precision.apply sends the same begin/move/end gesture for a typed value.
    before = coordinates()
    translation = translated_ball(urdf.scene, x=0.010)
    gizmo(user, urdf.scene).move(**translation).release(**translation)
    assert not urdf._tcp_ik_missed
    assert coordinates() == pytest.approx([before[0] + 10, *before[1:]], abs=0.00051)
    assert textarea.value.count("from waldoctl.setup import Pose") == 1
    dragged = textarea.value
    user.find(marker="object-undo").click()
    assert textarea.value == stored
    await edit()
    storage_checked("frame:fixture")
    user.find(marker="object-redo").click()
    assert textarea.value == dragged
    await edit()
    storage_checked("frame:fixture")
    assert coordinates() == pytest.approx([before[0] + 10, *before[1:]], abs=0.00051)
    user.find(marker="object-undo").click()
    assert textarea.value == stored
    await edit()
    storage_checked("frame:fixture")
    user.find(marker="scene-target-store-world").click()
    assert coordinates() == pytest.approx(world.as_list(), abs=0.005)
    assert "resolve(Pose" not in textarea.value
    user.find(marker="object-undo").click()
    assert textarea.value == stored
    moved = setup.with_frame("fixture", Frame((110, 200, 30, 15, 20, 35)))
    store.save("bench", moved)
    target = await preview()
    expected_world = moved.resolve(Pose(tuple(coordinates()), "fixture")).as_list()
    assert target.pose[:3] == pytest.approx(
        [v / 1000 for v in expected_world[:3]], abs=1e-4
    )
    assert target.pose[0] == pytest.approx(0.010, abs=1e-4)
    failed_line = ast.parse(stored).body[-1].lineno
    missing_frame = "Unknown frame 'fixture'"
    store.save("bench", SetupSnapshot())
    assert await wait_until(
        lambda: any(
            d["line"] == failed_line
            and d.get("severity") == "error"
            and d.get("source") == "simulation"
            and missing_frame in d["message"]
            for d in textarea.diagnostics
        ),
        10,
    )
    location = f'File "simulation_script.py", line {failed_line},'
    assert any(
        entry.stream == "stderr"
        and entry.text.startswith("[SIM ERROR]")
        and missing_frame in entry.text
        and location in entry.text
        for entry in program.log.entries
    )
    assert all(t.line_number != failed_line for t in program.dry_run.targets)
    assert textarea.value == stored
    # Only this deliberately failed preview is exempt from the ERROR-log gate.
    records = caplog.get_records("call")
    expected = [
        r
        for r in records
        if r.name == "waldo_commander.services.path_visualizer"
        and r.levelname == "ERROR"
        and r.getMessage().startswith("Simulation error (sim_id=")
        and missing_frame in r.getMessage()
        and location in r.getMessage()
    ]
    assert expected
    records[:] = [r for r in records if r not in expected]


@pytest.mark.integration
async def test_multiline_target_edit_preserves_source_and_rejects_stale_edits(
    user: User,
) -> None:
    urdf = await _open(user)
    source = """from parol6 import RobotClient
rbt = RobotClient()
# Fixture approach · µm notes stay intact
rbt.move_j(
    angles=[
        85.0,  # J1 starts here
        -85.0, 175.0, 5.0, 5.0, 175.0,
    ],
    speed=0.5,  # gentle approach
)
"""
    textarea = ui_state.active_textarea
    textarea.value = source
    await simulation.run_simulation()
    program = waldoctl.commander.programs.active
    assert await wait_until(lambda: bool(program.dry_run.targets), 15)
    target = program.dry_run.targets[-1]
    assert ui_state.editor_panel.target_edit_reason(target.id) is None
    assert await wait_until(
        lambda: program.dry_run.commanded_revision == program.dry_run.revision, 5
    )
    right_click(user, urdf.scene, [f"target:{target.id}"])
    user.find(marker="scene-edit-target").click()
    assert await wait_until(lambda: urdf._edit_active, 5)
    # A release writes the target, and the edit stays open for the next.
    start = urdf._requested_edit_pose
    translation = translated_ball(urdf.scene, x=0.005)
    gizmo(user, urdf.scene).move(**translation).release(**translation)
    assert not urdf._tcp_ik_missed
    assert urdf._requested_edit_pose == pytest.approx((start[0] + 0.005, *start[1:]))
    reached = [math.degrees(q) for q in urdf.get_editing_angles()]
    updated = textarea.value
    assert updated != source
    call = ast.parse(updated).body[2].value
    coordinates = ast.literal_eval(
        next(k.value for k in call.keywords if k.arg == "angles")
    )
    assert coordinates == pytest.approx(reached, abs=0.01)
    assert coordinates != pytest.approx([85, -85, 175, 5, 5, 175], abs=0.01)
    assert "# J1 starts here" in updated
    assert "speed=0.5,  # gentle approach" in updated
    assert "# Fixture approach · µm notes stay intact" in updated
    assert len(updated.splitlines()) == len(source.splitlines())
    assert urdf._edit_active
    # Its own write does not end the edit: the next release writes again.
    translation = translated_ball(urdf.scene, x=0.005)
    gizmo(user, urdf.scene).move(**translation).release(**translation)
    assert urdf._edit_active and textarea.value not in (source, updated)
    updated = textarea.value
    urdf.cancel_target_edit()
    assert not urdf._edit_active and textarea.value == updated

    await simulation.run_simulation()
    target = program.dry_run.targets[-1]
    assert await wait_until(
        lambda: program.dry_run.commanded_revision == program.dry_run.revision, 5
    )
    right_click(user, urdf.scene, [f"target:{target.id}"])
    user.find(marker="scene-edit-target").click()
    assert urdf._edit_active
    external = updated.replace("speed=0.5", "speed=0.3")
    textarea.value = external
    assert await wait_until(lambda: not urdf._edit_active, 5)
    assert textarea.value == external


@pytest.mark.integration
async def test_target_drag_keeps_motion_kind_and_live_robot_stationary(
    user: User,
    monkeypatch,
) -> None:
    urdf = await _open(user)
    textarea = ui_state.active_textarea
    source = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5)
rbt.move_l(pose=[0, 280, 250, 90, 0, 90], speed=0.5)
"""
    textarea.value = source
    await simulation.run_simulation()
    program = waldoctl.commander.programs.active
    assert await wait_until(
        lambda: program.dry_run.commanded_revision == program.dry_run.revision, 5
    )
    target = program.dry_run.targets[-1]
    right_click(user, urdf.scene, [f"target:{target.id}"])
    user.find(marker="scene-edit-target").click()
    assert urdf._edit_active
    live = waldoctl.commander.status.joints.angles.rad.copy()
    start = urdf._requested_edit_pose
    translation = translated_ball(urdf.scene, x=0.005)
    drag = gizmo(user, urdf.scene).move(**translation)
    assert await held_until(drag, lambda: urdf._requested_edit_pose != start, 5)
    drag.abort()
    assert urdf._requested_edit_pose == start
    # A cancelled drag writes nothing.
    assert textarea.value == source
    # A released one keeps the target's Cartesian form.
    gizmo(user, urdf.scene).move(**translation).release(**translation)
    assert textarea.value != source
    call = ast.parse(textarea.value).body[-1].value
    assert call.func.attr == "move_l"
    coordinates = ast.literal_eval(
        next(k.value for k in call.keywords if k.arg == "pose")
    )
    assert coordinates == pytest.approx([5, 280, 250, 90, 0, 90], abs=0.001)
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)
    urdf.cancel_target_edit()

    await simulation.run_simulation()
    assert await wait_until(
        lambda: program.dry_run.commanded_revision == program.dry_run.revision, 5
    )
    assert program.dry_run.commanded.stop == "completed"
    target = program.dry_run.targets[-1]
    right_click(user, urdf.scene, [f"target:{target.id}"])
    user.find(marker="scene-edit-target").click()
    assert urdf._edit_active
    before = textarea.value
    written = urdf._requested_edit_pose
    translation = translated_ball(urdf.scene, x=10)
    impossible = gizmo(user, urdf.scene).move(**translation)
    impossible.release(**translation)
    # Out of reach, the release goes back to where the target was written.
    assert urdf._edit_active and textarea.value == before
    assert urdf._requested_edit_pose == written
    right_click(user, urdf.scene, ["gizmo:tcp"])
    from waldo_commander.services.urdf_scene.ik_solver import EditingIKResult

    with monkeypatch.context() as unreachable:
        unreachable.setattr(
            urdf._ik_solver,
            "solve",
            lambda target_pos, current_angles, target_orientation: EditingIKResult(
                False, current_angles, float("inf"), 0
            ),
        )
        user.find(marker="scene-gizmo-vertical").click()
    assert not urdf._keep_tool_vertical
    assert not urdf.scene.interaction["alignment"]["vertical"]
    assert urdf._requested_edit_pose == written
    assert textarea.value == before

    right_click(user, urdf.scene, ["gizmo:tcp"])
    epoch = urdf.scene.gestures.epoch
    with monkeypatch.context() as displaced:
        displaced.setattr(ui_state, "active_client_id", "replacement-browser")
        user.find(marker="scene-gizmo-vertical").click()
        assert not urdf._keep_tool_vertical
        assert textarea.value == before
        assert urdf.scene.gestures.epoch == epoch
    await ui_state.control_panel.move_joint_to_angle(0, math.degrees(live[0]) + 10)
    assert await ui_state.control_panel.client.wait_motion(timeout=5, settle_window=0.2)
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live, abs=0.002)
    urdf.cancel_target_edit()


@pytest.mark.integration
async def test_blended_and_other_tool_previews_are_not_rewritten(user: User) -> None:
    from tests.helpers.editor_events import _fire_editor_event, _set_cursor_line

    urdf = await _open(user)
    textarea = ui_state.active_textarea
    program = waldoctl.commander.programs.active
    prefix = "from parol6 import RobotClient\nrbt = RobotClient()\n"
    for motions, expected in (
        (
            "rbt.move_j([85,-85,175,5,5,175], r=10)\nrbt.move_j([80,-85,175,5,5,175])\n",
            "Blended",
        ),
        (
            "rbt.move_j([85,-85,175,5,5,175])\nrbt.select_tool('SSG-48', 'pinch')\nrbt.move_j([80,-85,175,5,5,175])\n",
            "another tool",
        ),
    ):
        textarea.value = prefix + motions
        await simulation.run_simulation()
        assert await wait_until(
            lambda: program.dry_run.commanded_revision == program.dry_run.revision, 5
        )
        target = program.dry_run.targets[0 if expected == "Blended" else -1]
        reason = urdf._target_preview_reason(target.id)
        assert reason and expected in reason
        right_click(user, urdf.scene, [f"target:{target.id}"])
        user.find(marker="scene-edit-target").click()
        assert not urdf._edit_active
        assert textarea.value == prefix + motions

    # Capture Pose respects pose= on move_j, preserving the authored motion semantics.
    source = prefix + "rbt.move_j(pose=[0,280,250,90,0,90], speed=0.5)\n"
    textarea.value = source
    await simulation.run_simulation()
    assert await wait_until(
        lambda: program.dry_run.commanded_revision == program.dry_run.revision, 5
    )
    user.find(marker="tab-program").click()
    _fire_editor_event(
        textarea, "anchor-positions", {"anchors": dict(textarea._props["line-anchors"])}
    )
    _set_cursor_line(textarea, 3)
    assert await wait_until(
        lambda: ui_state.editor_panel._target_at_cursor() is not None, 5
    )
    user.find(marker="editor-capture-pose").click()
    call = ast.parse(textarea.value).body[-1].value
    assert call.func.attr == "move_j"
    assert {k.arg for k in call.keywords} == {"pose", "speed"}
    coords = ast.literal_eval(next(k.value for k in call.keywords if k.arg == "pose"))
    assert coords[:3] == pytest.approx(
        [getattr(waldoctl.commander.status.pose, axis) for axis in ("x", "y", "z")],
        abs=0.01,
    )
