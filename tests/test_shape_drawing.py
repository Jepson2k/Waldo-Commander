"""Draw primitives as one confirmed world/source transaction, including on a
face, and place them from the scene panel's card."""

import asyncio

import numpy as np
import pytest
import waldoctl
from nicegui.testing.user_interaction import UserInteraction
from scipy.spatial.transform import Rotation

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field, new_keepout, select
from tests.test_scene_jog_handles import _open, _start_recording
from waldo_commander.state import ui_state


def _draw_box(user, view):
    """Start drawing a box on the floor from the scene panel's card."""
    new_keepout(user, view, [0.4, 0.1, 0], "box", "keepout-draw")


@pytest.mark.integration
async def test_draw_floor_face_cancel_stale_and_recorded_undo(user):
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)
    live = waldoctl.commander.status.joints.angles.rad.copy()
    editor = await _start_recording(user)
    original_source = editor.value
    _draw_box(user, view)
    state = view.scene.interaction["shapeDraw"]
    Drag(user, view.scene, "draw").begin(**state).abort()
    assert not handle.shapes
    assert view._drawing is not None
    UserInteraction(user, {view.scene}, None).trigger(
        "draw_cancel", {"session": state["session"]}
    )
    assert view._drawing is None

    for invalid in (0, -1, float("nan"), float("inf")):
        _draw_box(user, view)
        state = view.scene.interaction["shapeDraw"]
        Drag(user, view.scene, "draw").begin(**state).release(
            session=state["session"], u=0, v=0, width=invalid, depth=0.1, height=0.1
        )
        await asyncio.sleep(0)
        assert not handle.shapes

    _draw_box(user, view)
    state = view.scene.interaction["shapeDraw"]
    Drag(user, view.scene, "draw").begin(**state).release(
        session=state["session"], u=0.05, v=0.03, width=0.1, depth=0.06, height=0.08
    )
    await confirmed(view, lambda: len(handle.shapes) == 1)
    fixture = handle.shapes[0]
    assert fixture.pose[:3] == pytest.approx([0.45, 0.13, 0.04])
    # The card that started the drawing closes once its shape is made.
    assert view._keepout_card is None
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
        user.find(marker="scene-face-keepout").click()
        assert user.find(marker="keepout-surface").elements.pop().text == (
            f"Face of {fixture.name}"
        )
        user.find(marker="keepout-shape").elements.pop().set_value(kind)
        user.find(marker="keepout-draw").click()
        state = view.scene.interaction["shapeDraw"]
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
    _draw_box(user, view)
    select(user, view, f"shape:{fixture.name}")
    assert view._drawing is None
    assert not view._hover_suspended
    _draw_box(user, view)
    right_click(user, view.scene, [f"shape:{fixture.name}"], [0.4, 0.1, 0])
    user.find(marker="shape-menu-move").click()
    assert view._drawing is None
    assert view._shape_move_active == f"shape:{fixture.name}"
    _draw_box(user, view)
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-target-at-robot").click()
    assert view._drawing is None
    assert view._edit_active and view._hover_suspended
    view.cancel_target_edit()
    _draw_box(user, view)
    state = view.scene.interaction["shapeDraw"]
    drag = Drag(user, view.scene, "draw").begin(**state)
    editor.value += "\n# external edit\n"
    view.check_drag_context()
    assert view._drawing is None
    drag.release(session=state["session"], u=0, v=0, width=0.1, depth=0.1, height=0.1)
    await asyncio.sleep(0)
    assert handle.shapes == before
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    _draw_box(user, view)
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
        "Undo scene edit · Ctrl+Z"
    )


@pytest.mark.integration
async def test_add_keepout_card_places_at_the_click_and_proposes_installation(user):
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)
    panel = ui_state.scene_panel
    await user.should_see(marker="scene-robot")
    user.find(marker="scene-close").click()
    assert not panel.is_open

    # Add keep-out… opens the scene panel on the card for the clicked point.
    right_click(user, view.scene, [], [0.35, -0.12, 0])
    user.find(marker="scene-create-keepout").click()
    await user.should_see(marker="keepout-card")
    assert panel.is_open
    card = user.find(marker="keepout-card").elements.pop()
    assert panel.body in card.ancestors()
    assert user.find(marker="keepout-surface").elements.pop().text == (
        "Floor at 350, -120 mm"
    )

    # Draw swaps Place and Draw for the drawing's controls: its height can be
    # typed once the view says the footprint is drawn. Cancel ends the
    # drawing and brings Place and Draw back.
    user.find(marker="keepout-draw").click()
    assert view._drawing is not None
    await user.should_see(marker="keepout-cancel")
    await user.should_not_see(marker="keepout-place")
    height = user.find(marker="keepout-height").elements.pop()
    assert not height.enabled
    UserInteraction(user, {view.scene}, None).trigger(
        "draw_phase",
        {
            "session": view._drawing["session"],
            "phase": "height",
            "width": 0.1,
            "depth": 0.08,
            "height": 0.12,
            "step": 0.005,
        },
    )
    assert height.enabled and height.value == 120
    assert "5 mm steps" in user.find(marker="keepout-hint").elements.pop().text
    user.find(marker="keepout-cancel").click()
    assert view._drawing is None
    await user.should_see(marker="keepout-place")

    user.find(marker="keepout-shape").elements.pop().set_value("sphere")
    user.find(marker="keepout-place").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    sphere = handle.shapes[0]
    assert sphere.kind == "sphere"
    assert sphere.pose == pytest.approx((0.35, -0.12, sphere.radius, 0, 0, 0))
    assert view._selected_objects == [f"shape:{sphere.name}"]
    await user.should_not_see(marker="keepout-card")
    assert card.is_deleted

    # Add installation… proposes the shape for the installation layer, and
    # leaves the program's keep-outs as they are.
    right_click(user, view.scene, [], [0.3, 0.2, 0])
    user.find(marker="scene-create-installation").click()
    user.find(marker="keepout-place").click()
    await confirmed(view, lambda: len(handle.installation_draft) == 1)
    proposed = handle.installation_draft[0]
    assert proposed.kind == "box"
    assert proposed.pose[:3] == pytest.approx((0.3, 0.2, proposed.z / 2))
    assert handle.shapes == [sphere]
    await user.should_not_see(marker="keepout-card")
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: not handle.installation_draft)
    assert handle.shapes == [sphere]
