"""Surface operations preserve live state, named coordinates and source history."""

import ast
import asyncio

import numpy as np
import pytest
import waldoctl
from nicegui.testing.user_interaction import UserInteraction
from waldoctl.setup import SetupSnapshot

from tests.helpers.scene_objects import confirmed, field, teaching_fixture
from tests.helpers.wait import wait_until
from tests.test_scene_jog_handles import _open
from waldo_commander.state import ui_state
from waldo_commander.components.simulation_engine import simulation


def surface_context(user, view, fixture, point):
    UserInteraction(user, {view.scene}, None).trigger(
        "context",
        {
            "epoch": view.scene.gestures.epoch,
            "gen": 1,
            "cx": 100,
            "cy": 100,
            "hits": [f"shape:{fixture.name}"],
            "ground": [0.3, 0, 0],
            "surface": {
                "id": f"shape:{fixture.name}",
                **dict(zip("xyz", point.tolist())),
            },
        },
    )
    user.find(marker="scene-teach-surface").click()


@pytest.mark.integration
async def test_teach_face_constraints_named_sequence_invalid_and_stale(user):
    view = await _open(user)
    await confirmed(view)
    editor = ui_state.active_textarea
    source = "from parol6 import RobotClient\nrbt = RobotClient()\n# keep this note\n"
    editor.value = source
    fixture, point, normal = teaching_fixture(view)
    waldoctl.commander.scene.shapes = [fixture]
    await confirmed(view, lambda: waldoctl.commander.scene.shapes == [fixture])
    live = waldoctl.commander.status.joints.angles.rad.copy()
    surface_context(user, view, fixture, point)
    teaching = view._teaching
    assert teaching is not None and teaching.valid
    pose = teaching.snapshot.resolve("work")
    assert pose.values[:3] == pytest.approx((point + normal * 0.03) * 1000)
    assert pose.matrix()[:3, 2] == pytest.approx(-normal)
    assert view.scene.interaction["edit"] is None
    initial = list(view._editing_angles)
    for invalid in (-1, float("nan"), float("inf")):
        field(user, "surface-standoff", invalid)
        assert not teaching.valid
        assert editor.value == source
    field(user, "surface-standoff", 10000)
    assert not teaching.valid
    assert view._editing_angles == pytest.approx(initial)
    assert "unreachable" in teaching.status.text
    field(user, "surface-standoff", 30)
    assert teaching.valid
    field(user, "surface-twist", 5)
    assert teaching.valid
    assert teaching.snapshot.resolve("work").matrix()[:3, 2] == pytest.approx(-normal)
    user.find(marker="surface-flip").click()
    assert teaching.snapshot.resolve("work").matrix()[:3, 2] == pytest.approx(normal)
    user.find(marker="surface-flip").click()
    user.find(marker="surface-sequence").click()
    field(user, "surface-clearance", 5)
    assert teaching.valid
    user.find(marker="surface-insert").click()
    assert view._teaching is None and not view._edit_active
    assert "# keep this note" in editor.value
    tree = ast.parse(editor.value)
    assignment = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "from_dict"
    )
    snapshot = SetupSnapshot.from_dict(ast.literal_eval(assignment.value.args[0]))
    assert snapshot.resolve("work").values[:3] == pytest.approx(pose.values[:3])
    assert np.array(snapshot.resolve("approach").values[:3]) - snapshot.resolve(
        "work"
    ).values[:3] == pytest.approx(normal * 5, abs=1e-6)
    assert [
        n.value.func.attr
        for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
    ] == ["move_j", "move_l", "move_l"]
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    await simulation.run_simulation()
    program = waldoctl.commander.programs.active
    assert await wait_until(
        lambda: program.dry_run.commanded_revision == program.dry_run.revision, 15
    )
    assert program.dry_run.commanded.stop == "completed"
    record = program.dry_run.commanded
    motions = [b for b in record.blocks if b.rows and b.line_number]
    assert len(motions) == 3
    for block, name in zip(motions, ("approach", "work", "retract")):
        assert record.tcp[block.start_row + block.rows - 1, :3] * 1000 == pytest.approx(
            snapshot.resolve(name).values[:3], abs=0.1
        )

    surface_context(user, view, fixture, point)
    assert view._teaching
    editor.value += "\n# external source\n"
    assert await wait_until(lambda: view._teaching is None, 5)
    surface_context(user, view, fixture, point)
    assert view._teaching
    waldoctl.commander.scene.shapes = []
    await confirmed(view, lambda: not waldoctl.commander.scene.shapes)
    assert await wait_until(lambda: view._teaching is None, 5)
    await asyncio.sleep(0)
    assert not view._edit_active
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)


@pytest.mark.integration
async def test_tool_change_at_insert_cannot_commit_cancelled_teaching(
    user, monkeypatch
):
    view = await _open(user)
    await confirmed(view)
    editor = ui_state.active_textarea
    source = "from parol6 import RobotClient\nrbt = RobotClient()\n"
    editor.value = source
    fixture, point, _ = teaching_fixture(view)
    waldoctl.commander.scene.shapes = [fixture]
    await confirmed(view, lambda: waldoctl.commander.scene.shapes == [fixture])
    surface_context(user, view, fixture, point)
    assert view._teaching.valid
    client = ui_state.control_panel.client
    check = view.check_edit_context
    # The Insert callback is the first context check after real tool readback.
    monkeypatch.setattr(view, "check_edit_context", lambda: view._edit_active)
    try:
        assert await client.wait_command(
            await client.select_tool("SSG-48", "pinch"), timeout=5
        )
        assert await wait_until(
            lambda: waldoctl.commander.status.tool.key == "SSG-48", 5
        )
        monkeypatch.setattr(view, "check_edit_context", check)
        user.find(marker="surface-insert").click()
        assert view._teaching is None
        assert editor.value == source
    finally:
        monkeypatch.setattr(view, "check_edit_context", check)
        view.cancel_target_edit()
        assert await client.wait_command(await client.select_tool("NONE"), timeout=5)
