"""Resize primitives through scene gestures, world acknowledgment and history."""

import asyncio
import math

import numpy as np
import pytest
import waldoctl
from scipy.spatial.transform import Rotation

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field
from tests.test_scene_jog_handles import _open


@pytest.mark.integration
async def test_primitive_resize_faces_center_radius_cancel_and_history(user):
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)
    live = waldoctl.commander.status.joints.angles.rad.copy()

    for kind, axis, dimension in (
        ("box", 0, "x"),
        ("cylinder", 2, "length"),
        ("sphere", 1, "radius"),
    ):
        count = len(handle.shapes)
        right_click(user, view.scene, [], [0.4, 0.1, 0])
        user.find(marker=f"scene-add-{kind}").click()
        await confirmed(
            view,
            lambda: len(handle.shapes) == count + 1
            and handle.shapes[-1].kind == kind
            and view._selected_objects == [f"shape:{handle.shapes[-1].name}"],
        )
        user.find(marker="object-mode-translate").click()
        field(user, "object-rx", 20)
        await confirmed(
            view, lambda: math.isclose(handle.shapes[-1].pose[3], math.radians(20))
        )
        field(user, "object-rz", 35)
        await confirmed(
            view, lambda: math.isclose(handle.shapes[-1].pose[5], math.radians(35))
        )
        user.find(marker="object-mode-resize").click()
        initial = handle.shapes[-1]
        initial_count = len(view.object_history.undo_stack)

        def resize(size, symmetric=False, abort=False):
            spec = view.scene.interaction["shapeMove"]
            drag = Drag(user, view.scene, "shape").begin(
                **{**spec, "symmetric": symmetric},
                axis=axis,
                sign=-1,
            )
            if abort:
                drag.abort()
            else:
                drag.release(session=spec["session"], node=spec["node"], size=size)

        resize(0.2, abort=True)
        assert handle.shapes[-1] == initial
        for invalid in (0, -1, float("nan"), float("inf")):
            resize(invalid)
            await asyncio.sleep(0)
            assert handle.shapes[-1] == initial
        size = getattr(initial, dimension) + 0.04
        resize(size)
        await confirmed(view, lambda: getattr(handle.shapes[-1], dimension) == size)
        edited = handle.shapes[-1]
        direction = Rotation.from_euler("xyz", initial.pose[3:]).as_matrix()[:, axis]
        expected = (
            np.array(initial.pose[:3]) - direction * 0.02
            if kind != "sphere"
            else initial.pose[:3]
        )
        assert edited.pose[:3] == pytest.approx(expected)
        assert edited.pose[3:] == initial.pose[3:]
        assert len(view.object_history.undo_stack) == initial_count + 1
        user.find(marker="object-undo").click()
        await confirmed(view, lambda: handle.shapes[-1] == initial)
        user.find(marker="object-redo").click()
        await confirmed(view, lambda: handle.shapes[-1] == edited)
        resize(size + 0.02, symmetric=True)
        await confirmed(
            view, lambda: getattr(handle.shapes[-1], dimension) == size + 0.02
        )
        assert handle.shapes[-1].pose == edited.pose
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)
