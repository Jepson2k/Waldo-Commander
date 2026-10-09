"""Shared steps for the scene's object-editing ``user`` tests."""

import numpy as np
import waldoctl
from nicegui.testing.user_interaction import UserInteraction
from scipy.spatial.transform import Rotation
from waldoctl.shapes import Box

from tests.helpers.wait import wait_until


def select(user, view, *names, extend=False):
    """Select ``names`` in the scene as a click (or Shift-click) would."""
    UserInteraction(user, {view.scene}, None).trigger(
        "select",
        {
            "revision": waldoctl.commander.scene.revision,
            "hits": [{"id": name} for name in names],
            "extend": extend,
        },
    )


def field(user, marker, value):
    """Enter ``value`` in the field marked ``marker`` and leave it."""
    user.find(marker=marker).elements.pop().set_value(value)
    user.find(marker=marker).trigger("blur")


async def confirmed(view, predicate=lambda: True):
    """Wait until the world is confirmed, no object edit is pending and
    ``predicate`` holds."""
    assert await wait_until(
        lambda: waldoctl.commander.scene.confirmed
        and not view.object_history.busy
        and predicate(),
        8,
    ), "the object edit was never confirmed"


def teaching_fixture(view):
    """A thin box whose top face sits 3 cm in front of the tool, facing it;
    with the face point and its outward normal."""
    view._ensure_ik_solver()
    fk = view._ik_solver.forward_kinematics(view._get_robot_angles_rad()).copy()
    rotation = Rotation.from_euler("XYZ", fk[3:]).as_matrix()
    normal = -rotation[:, 2]
    point = fk[:3] - normal * 0.03
    frame = rotation @ np.diag([1, -1, -1])
    fixture = Box(
        name="Teaching-face",
        collision=False,
        x=0.12,
        y=0.1,
        z=0.04,
        pose=(
            *list(point - normal * 0.02),
            *Rotation.from_matrix(frame).as_euler("xyz"),
        ),
    )
    return fixture, point, normal
