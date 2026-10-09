"""A selection transforms as one authored edit, independently of the robot."""

import asyncio
import math
from dataclasses import replace

import numpy as np
import pytest
import waldoctl
from scipy.spatial.transform import Rotation

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field, select
from tests.test_scene_jog_handles import _open, _start_recording


@pytest.mark.integration
async def test_selection_move_rotate_history_and_stale_rejection(user):
    view = await _open(user)
    await confirmed(view)
    handle = waldoctl.commander.scene
    live = waldoctl.commander.status.joints.angles.rad.copy()
    editor = await _start_recording(user)
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-add-box").click()
    field(user, "shape-dialog-name", "Fixture")
    user.find(marker="shape-dialog-save").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    user.find(marker="object-orientation").elements.pop().open()
    field(user, "object-rx", 20)
    await confirmed(view, lambda: handle.shapes[0].pose[3] == math.radians(20))
    assert user.find(marker="object-orientation").elements.pop().value
    field(user, "object-ry", 10)
    await confirmed(view, lambda: handle.shapes[0].pose[4] == math.radians(10))
    assert user.find(marker="object-orientation").elements.pop().value
    user.find(marker="object-duplicate").click()
    await confirmed(view, lambda: len(handle.shapes) == 2)
    assert not user.find(marker="object-orientation").elements.pop().value
    second = handle.shapes[1].name
    field(user, "object-y", 300)
    await confirmed(view, lambda: handle.shapes[1].pose[1] == 0.3)
    select(user, view, "shape:Fixture")
    select(user, view, f"shape:{second}", extend=True)
    before, source = tuple(handle.shapes), editor.value
    history_count = len(view.object_history.undo_stack)
    center = np.mean([s.pose[:3] for s in before], axis=0)
    arrows = view.scene.interaction["shapeMove"]
    assert len(arrows["members"]) == 2
    assert arrows["center"] == pytest.approx(center)
    Drag(user, view.scene, "shape").begin(**arrows).abort()
    assert tuple(handle.shapes) == before and editor.value == source
    moved_center = center + [0.1, -0.05, 0.03]
    Drag(user, view.scene, "shape").begin(**arrows).release(
        **arrows, **dict(zip("xyz", moved_center))
    )
    await confirmed(
        view, lambda: len(view.object_history.undo_stack) == history_count + 1
    )
    moved = tuple(handle.shapes)
    for original, actual in zip(before, moved):
        assert actual.pose[:3] == pytest.approx(
            np.array(original.pose[:3]) + moved_center - center
        )
        assert actual.pose[3:] == pytest.approx(original.pose[3:])
    moved_source = editor.value
    arrows = view.scene.interaction["shapeMove"]
    arrows = {**arrows, "mode": "rotate"}
    turn = Rotation.from_euler("z", 90, degrees=True)
    Drag(user, view.scene, "shape").begin(**arrows).release(
        **arrows,
        **dict(zip("xyz", moved_center)),
        **dict(zip(("qx", "qy", "qz", "qw"), turn.as_quat())),
    )
    await confirmed(
        view, lambda: len(view.object_history.undo_stack) == history_count + 2
    )
    rotated = tuple(handle.shapes)
    rotated_source = editor.value
    for original, actual in zip(moved, rotated):
        assert actual.params() == original.params()
        assert actual.pose[:3] == pytest.approx(
            moved_center + turn.apply(np.array(original.pose[:3]) - moved_center)
        )
        assert Rotation.from_euler("xyz", actual.pose[3:]).as_matrix() == pytest.approx(
            (turn * Rotation.from_euler("xyz", original.pose[3:])).as_matrix()
        )
    for expected, expected_source in ((moved, moved_source), (before, source)):
        user.find(marker="object-undo").click()
        await confirmed(view, lambda: tuple(handle.shapes) == expected)
        assert editor.value == expected_source
    for expected, expected_source in ((moved, moved_source), (rotated, rotated_source)):
        user.find(marker="object-redo").click()
        await confirmed(view, lambda: tuple(handle.shapes) == expected)
        assert editor.value == expected_source

    select(user, view, "shape:Fixture")
    user.find(marker="object-lock").click()
    select(user, view, f"shape:{second}", extend=True)
    assert view.scene.interaction["shapeMove"] is None
    user.find(marker="object-lock").click()
    user.find(marker="object-lock").click()
    select(user, view, "shape:Fixture")
    select(user, view, f"shape:{second}", extend=True)
    arrows = view.scene.interaction["shapeMove"]
    stale = Drag(user, view.scene, "shape").begin(**arrows)
    external = [
        replace(handle.shapes[0], pose=(0.8, 0, 0.1, 0, 0, 0)),
        handle.shapes[1],
    ]
    handle.shapes = external
    await confirmed(view, lambda: handle.shapes == external)
    stale.release(**arrows, x=5, y=5, z=5, qx=0, qy=0, qz=0, qw=1)
    # An accepted release commits in a task; let it start before waiting.
    await asyncio.sleep(0)
    await confirmed(view)
    assert handle.shapes == external
    assert not view.object_history.undo_stack
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)
