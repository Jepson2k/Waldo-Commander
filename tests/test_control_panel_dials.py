"""Control panel: joint dials, level chips and the zoom-following jog step."""

from __future__ import annotations

import math
import re
import time
from typing import TYPE_CHECKING

import pytest
import waldoctl
from nicegui import app
from nicegui.testing import User
from waldoctl import ActionState

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.browser_helpers import js
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    screen_wait_for_scene_ready,
    simulate_click,
    teleport_to_jog_pose,
    wait_for_app_ready,
    wait_for_motion_start,
    wait_until,
)
from waldo_commander.components.control import DIAL_RADIUS, DIAL_SIZE, dial_geometry
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

R = DIAL_RADIUS


def _point(deg: float, r: float = R) -> tuple[float, float]:
    c = DIAL_SIZE / 2
    return c + r * math.sin(math.radians(deg)), c - r * math.cos(math.radians(deg))


def _numbers(path: str) -> list[float]:
    return [float(t) for t in re.findall(r"-?\d+(?:\.\d+)?", path)]


def _arc(path: str) -> dict:
    """The one arc of ``M x0 y0 A r r 0 large sweep x1 y1``."""
    n = _numbers(path)
    assert len(n) == 9, f"not a single arc: {path!r}"
    return {"start": (n[0], n[1]), "large": n[5], "sweep": n[6], "end": (n[7], n[8])}


def _close(a: tuple[float, float], b: tuple[float, float], tol: float = 0.02) -> bool:
    return math.dist(a, b) <= tol


@pytest.mark.unit
def test_dial_geometry_draws_travel_from_zero_clamped_into_the_limits() -> None:
    # J3's range lies entirely above zero: the fill starts at the low limit and
    # runs clockwise to the angle, where the knob sits.
    j3 = dial_geometry(107.866, 287.8675, 180.0, R)
    fill = _arc(j3.fill)
    assert _close(fill["start"], _point(107.866)) and _close(fill["end"], _point(180.0))
    assert fill["sweep"] == 1 and fill["large"] == 0
    assert _close(j3.knob, _point(180.0))
    track = _arc(j3.track)
    assert _close(track["start"], _point(107.866))
    assert _close(track["end"], _point(287.8675))
    assert track["large"] == 1, "180.0015° of travel is just over half a turn"
    assert len(_numbers(j3.ticks)) == 8, "a tick segment at each limit"

    # J2's range lies below zero: the fill starts at the high limit and runs
    # counter-clockwise to the angle.
    j2 = dial_geometry(-145.0088, -3.375, -90.0, R)
    fill = _arc(j2.fill)
    assert _close(fill["start"], _point(-3.375)) and _close(fill["end"], _point(-90.0))
    assert fill["sweep"] == 0
    assert _close(j2.knob, _point(-90.0))

    # J6 covers a whole turn: the track is a full circle drawn as two arcs,
    # without limit ticks, and a full-turn fill closes on itself.
    j6 = dial_geometry(0.0, 360.0, 90.0, R)
    assert j6.track.count("A") == 2 and _numbers(j6.track)[:2] == list(_point(0.0))
    assert j6.ticks == ""
    quarter = _arc(j6.fill)
    assert _close(quarter["end"], _point(90.0)) and _close(j6.knob, _point(90.0))
    assert dial_geometry(0.0, 360.0, 360.0, R).fill == j6.track

    # J1 straddles zero: at zero there is nothing to fill and the knob is at the
    # top; an angle past a limit is drawn at the limit; an unknown angle sits at
    # the fill's origin.
    j1 = dial_geometry(-123.05, 123.05, 0.0, R)
    assert j1.fill == "" and _close(j1.knob, _point(0.0))
    assert _close(dial_geometry(-123.05, 123.05, 200.0, R).knob, _point(123.05))
    unknown = dial_geometry(-123.05, 123.05, math.nan, R)
    assert unknown.fill == "" and _close(unknown.knob, _point(0.0))


@pytest.mark.integration
async def test_a_jog_click_redraws_only_the_dial_of_the_joint_that_moved(
    user: User,
) -> None:
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    cp = ui_state.control_panel
    await teleport_to_jog_pose(cp.client)

    assert len(cp._dials) == ui_state.active_robot.joints.count
    assert await wait_until(
        lambda: all(math.isfinite(d.last_deg) for d in cp._dials)
    ), "the dials never took their first angles from the status loop"
    j1, j2 = cp._dials[0], cp._dials[1]

    def props(dial) -> tuple[str, str, str]:
        return dial.fill._props["d"], dial.knob._props["cx"], dial.knob._props["cy"]

    j1_before, j2_before = props(j1), props(j2)
    waldoctl.commander.settings.jog.joint_step_deg = 5.0
    await simulate_click(user, "btn-j1-plus")
    await wait_for_motion_start()
    assert await wait_until(lambda: props(j1) != j1_before, timeout_s=10.0), (
        "J1 moved but its dial did not redraw"
    )
    assert await wait_until(
        lambda: waldoctl.commander.status.action.state == ActionState.IDLE,
        timeout_s=15.0,
    )
    assert props(j2) == j2_before, "J2 did not move, so its dial must not redraw"

    # The redrawn dial shows the live angle: its knob is where the geometry
    # puts the current J1 angle, within the redraw threshold.
    live = float(waldoctl.commander.status.joints.angles.deg[0])
    expected = dial_geometry(j1.lo, j1.hi, live, R).knob
    knob = (float(j1.knob._props["cx"]), float(j1.knob._props["cy"]))
    assert math.dist(knob, expected) < 0.3, f"knob {knob} vs live angle {live:.2f}°"
    end = _arc(j1.fill._props["d"])["end"]
    assert math.dist(end, expected) < 0.3, "the fill ends under the knob"


@pytest.mark.integration
async def test_level_chip_popover_sets_the_speed_text_and_storage(user: User) -> None:
    await user.open("/")
    await wait_for_app_ready()
    cp = ui_state.control_panel
    refs = cp._rating_widgets["jog_speed"]
    original = waldoctl.commander.settings.jog.speed
    try:
        # The popover is Quasar's; picking the seventh dot in it is the rating's
        # own change event.
        user.find(marker="rating-jog-speed").trigger("update:modelValue", 7)
        assert waldoctl.commander.settings.jog.speed == 70
        assert app.storage.general["jog_speed"] == 70
        assert refs["label"].text == "70%"
        assert "70%" in refs["tooltip"].text
    finally:
        cp.adjust_rating("jog_speed", original - waldoctl.commander.settings.jog.speed)


@pytest.mark.integration
async def test_jog_step_follows_the_camera_until_pinned_and_in_the_tabs_unit(
    user: User,
) -> None:
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    cp = ui_state.control_panel
    jog = waldoctl.commander.settings.jog
    field = user.find(marker="step-input")

    cp.on_camera_distance(1.2)
    assert jog.joint_step_deg == 1.0 and cp.step.is_auto
    await user.should_see(marker="step-auto")

    # A typed value pins the step; the camera no longer moves it.
    field.clear()
    field.type("2.5")
    assert jog.joint_step_deg == 2.5 and not cp.step.is_auto
    cp.on_camera_distance(0.4)
    assert jog.joint_step_deg == 2.5
    await user.should_not_see(marker="step-auto")

    # Clearing the field returns to auto, at the band the camera is in now.
    field.clear()
    assert jog.joint_step_deg == 0.1 and cp.step.is_auto
    await user.should_see(marker="step-auto")

    # The cartesian tab reads the same band in millimetres, and a click moves
    # the tool by exactly that.
    user.find(marker="tab-cartesian").click()
    assert jog.joint_step_deg == 0.5
    await teleport_to_jog_pose(cp.client)
    assert await wait_until(
        lambda: waldoctl.commander.status.action.state == ActionState.IDLE,
        timeout_s=10.0,
    )
    initial_z = float(waldoctl.commander.status.pose.z)
    await simulate_click(user, "axis-zplus")
    await wait_for_motion_start()
    assert await wait_until(
        lambda: waldoctl.commander.status.action.state == ActionState.IDLE,
        timeout_s=15.0,
    )
    moved = float(waldoctl.commander.status.pose.z) - initial_z
    assert 0.4 <= moved <= 0.6, f"expected +0.5 mm, moved {moved:.3f} mm"


@pytest.mark.browser
@skip_webgl_macos_ci
def test_camera_distance_reaches_the_step_within_a_second(screen: Screen) -> None:
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40.0)
    cp = ui_state.control_panel

    deadline = time.monotonic() + 10.0
    while cp.step.camera_distance_m is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert cp.step.camera_distance_m is not None, "the scene never reported its camera"

    # Pull the camera out to 2 m from its target, the way a zoom-out lands.
    js(
        screen,
        "const c = getElement(document.querySelector('.nicegui-scene'));"
        "const t = c.controls.target;"
        "c.camera.position.sub(t).setLength(2.0).add(t);",
    )
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and abs(cp.step.camera_distance_m - 2.0) > 0.05:
        time.sleep(0.05)
    assert abs(cp.step.camera_distance_m - 2.0) <= 0.05, cp.step.camera_distance_m
    assert waldoctl.commander.settings.jog.joint_step_deg == 5.0
