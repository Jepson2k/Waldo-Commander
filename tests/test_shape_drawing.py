"""Draw primitives as one confirmed world/source transaction, including on a face."""

import asyncio

import numpy as np
import pytest
import waldoctl
from nicegui.testing.user_interaction import UserInteraction
from scipy.spatial.transform import Rotation

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field, select
from tests.test_scene_jog_handles import _open, _start_recording


@pytest.mark.integration
async def test_draw_floor_face_cancel_stale_and_recorded_undo(user):
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)
    live = waldoctl.commander.status.joints.angles.rad.copy()
    editor = await _start_recording(user)
    original_source = editor.value
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-draw-box").click()
    state = view.scene.interaction["shapeDraw"]
    Drag(user, view.scene, "draw").begin(**state).abort()
    assert not handle.shapes
    assert view._drawing is not None
    UserInteraction(user, {view.scene}, None).trigger(
        "draw_cancel", {"session": state["session"]}
    )
    assert view._drawing is None

    for invalid in (0, -1, float("nan"), float("inf")):
        right_click(user, view.scene, [], [0.4, 0.1, 0])
        user.find(marker="scene-draw-box").click()
        state = view.scene.interaction["shapeDraw"]
        Drag(user, view.scene, "draw").begin(**state).release(
            session=state["session"], u=0, v=0, width=invalid, depth=0.1, height=0.1
        )
        await asyncio.sleep(0)
        assert not handle.shapes

    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-draw-box").click()
    state = view.scene.interaction["shapeDraw"]
    Drag(user, view.scene, "draw").begin(**state).release(
        session=state["session"], u=0.05, v=0.03, width=0.1, depth=0.06, height=0.08
    )
    await confirmed(view, lambda: len(handle.shapes) == 1)
    fixture = handle.shapes[0]
    assert fixture.pose[:3] == pytest.approx([0.45, 0.13, 0.04])
    assert "set_shapes(" in editor.value
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: not handle.shapes)
    assert editor.value == original_source
    user.find(marker="object-redo").click()
    await confirmed(view, lambda: handle.shapes == [fixture])
    field(user, "object-rx", 25)
    await confirmed(view, lambda: handle.shapes[0].pose[3] > 0.4)
    fixture = handle.shapes[0]
    rotation = Rotation.from_euler("xyz", fixture.pose[3:])
    normal = rotation.apply([0, 0, 1])
    face_point = np.array(fixture.pose[:3]) + normal * fixture.z / 2
    for kind in ("cylinder", "sphere"):
        UserInteraction(user, {view.scene}, None).trigger(
            "context",
            {
                "epoch": view.scene.gestures.epoch,
                "gen": 1,
                "cx": 100,
                "cy": 100,
                "hits": [f"shape:{fixture.name}"],
                "ground": [0.4, 0.1, 0],
                "surface": {
                    "id": f"shape:{fixture.name}",
                    **dict(zip("xyz", face_point.tolist())),
                },
            },
        )
        user.find(marker=f"scene-draw-{kind}").click()
        state = view.scene.interaction["shapeDraw"]
        assert state["label"] == f"Face of {fixture.name}"
        count = len(handle.shapes)
        Drag(user, view.scene, "draw").begin(**state).release(
            session=state["session"], u=0, v=0, width=0.04, depth=0.04, height=0.06
        )
        await confirmed(view, lambda: len(handle.shapes) == count + 1)
        shape = handle.shapes[-1]
        height = 0.04 if kind == "sphere" else 0.06
        assert shape.pose[:3] == pytest.approx(face_point + normal * height / 2)
        assert shape.radius == pytest.approx(0.02)
        assert Rotation.from_euler("xyz", shape.pose[3:]).apply(
            [0, 0, 1]
        ) == pytest.approx(normal)

    before = list(handle.shapes)
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-draw-box").click()
    select(user, view, f"shape:{fixture.name}")
    assert view._drawing is None
    assert not view._hover_suspended
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-draw-box").click()
    right_click(user, view.scene, [f"shape:{fixture.name}"], [0.4, 0.1, 0])
    user.find(marker="shape-menu-move").click()
    assert view._drawing is None
    assert view._shape_move_active == f"shape:{fixture.name}"
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-draw-box").click()
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-target-at-robot").click()
    assert view._drawing is None
    assert view._edit_active and view._hover_suspended
    view.cancel_target_edit()
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-draw-box").click()
    state = view.scene.interaction["shapeDraw"]
    drag = Drag(user, view.scene, "draw").begin(**state)
    editor.value += "\n# external edit\n"
    view.check_drag_context()
    assert view._drawing is None
    drag.release(session=state["session"], u=0, v=0, width=0.1, depth=0.1, height=0.1)
    await asyncio.sleep(0)
    assert handle.shapes == before
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-draw-box").click()
    state = view.scene.interaction["shapeDraw"]
    Drag(user, view.scene, "draw").begin(**state).release(
        session=state["session"], u=0, v=0, width=0.1, depth=0.1, height=0.1
    )
    editor.value += "\n# edit while the released drawing is being committed\n"
    await user.should_see("changed during this edit")
    await user.should_see(marker="scene-feedback")
    assert "text-wc-error" in user.find(marker="scene-feedback").elements.pop().classes
    assert not view._selected_objects
    assert handle.shapes == before

    select(user, view, f"shape:{fixture.name}")
    await user.should_not_see(marker="scene-feedback")
    await user.should_not_see(marker="object-feedback")
    field(user, "object-name", handle.shapes[1].name)
    await user.should_see(marker="object-feedback")
    assert "text-wc-error" in user.find(marker="object-feedback").elements.pop().classes
    field(user, "object-dim-x", fixture.x * 1000 + 10)
    await confirmed(view, lambda: handle.shapes[0].x > fixture.x)
    await user.should_not_see(marker="object-feedback")
    assert user.find(marker="object-undo").elements.pop().enabled
    assert user.find(marker="object-undo-tooltip").elements.pop().text == (
        "Undo object edit · Ctrl+Z"
    )
