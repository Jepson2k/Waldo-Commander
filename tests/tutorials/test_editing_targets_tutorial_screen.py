"""Tutorial: editing a program's targets in the 3D view."""

import pytest
import waldoctl
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import run_in_app
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    PLACEMENT_VIEW,
    arm_pixel,
    center,
    menu_item,
    outward,
    program_panel,
)
from tests.helpers.demo_video import Tutorial
from tests.helpers.scene_screen import EDIT_PROGRAM, TCP_VIEW, probe, program_previewed
from tests.helpers.scene_surface import (
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]


def test_editing_targets(demo_screen):
    screen = demo_screen
    scene = ui_state.urdf_scene

    def source() -> str:
        return run_in_app(lambda: ui_state.active_textarea.value)

    with program_previewed(EDIT_PROGRAM) as targets, program_panel(screen):
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        wait_for_scene_idle(screen)
        live = waldoctl.commander.status.joints.angles.rad.copy()
        mx, my = scene_object_pixel(screen, f"targetgroup:{targets[-1]}")
        video = Tutorial(
            screen,
            "editing_targets",
            "Editing targets",
            "Move a program's targets in the 3D view; the code follows",
        )
        video.jump(*outward((mx, my), arm_pixel(screen)))

        video.step("Each move in the program has a target in the view")
        video.glide((mx, my), 1.2)
        video.pause(0.4)
        video.step("Right-click it and choose Edit Target", read=0.6)
        video.context_click()
        item = menu_item(screen, "Edit Target")
        video.pause(0.6)
        video.glide(center(item), 0.8)
        video.click(settle=lambda: wait(screen).until(lambda _: scene._edit_active))
        video.pause(0.8)

        initial = scene._requested_edit_pose
        x, y, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", "tcp:ball", "Z")
        )
        video.step("Drag the gizmo: the target moves, the arm stays where it is")
        video.glide((x, y), 0.8)
        video.press()
        video.glide((x + dx * 110, y + dy * 110), 3.0)
        video.release(
            settle=lambda: wait(screen).until(
                lambda _: run_in_app(lambda: scene._requested_edit_pose) != initial
            )
        )
        wait(screen).until(lambda _: source() != EDIT_PROGRAM)
        video.step("Let go and its line in the program is rewritten", read=2.0)

        video.step("Ctrl+Z puts it back", read=0.6)
        video.chord(
            Keys.CONTROL,
            "z",
            settle=lambda: wait(screen).until(lambda _: source() == EDIT_PROGRAM),
        )
        video.live(1.2)
        video.step("Escape finishes editing", read=0.6)
        video.key(
            Keys.ESCAPE,
            settle=lambda: wait(screen).until(lambda _: not scene._edit_active),
        )
        video.pause(0.8)

        scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
        wait_for_scene_idle(screen)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        video.jump(*outward((x, y), arm_pixel(screen)))
        video.step("To add a target, right-click where it should go")
        video.glide((x, y), 1.6)
        video.pause(0.4)
        video.context_click()
        item = menu_item(screen, "Place Target Here")
        video.pause(0.6)
        video.glide(center(item), 0.8)
        video.click(settle=lambda: wait(screen).until(lambda _: scene._edit_active))
        wait(screen).until(
            lambda _: source().count("move_") > EDIT_PROGRAM.count("move_")
        )
        video.step("A move to it is added to the program at once", read=1.4)

        tx, ty, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", "tcp:ball", "Z")
        )
        video.step("Lift it off the floor; red means the arm can't reach it")
        video.glide((tx, ty), 0.8)
        video.press()
        reached = video.along(
            [(tx, ty), (tx + dx * 260, ty + dy * 260)],
            6.0,
            until=lambda: run_in_app(
                lambda: scene._requested_edit_pose[2] > 0.04
                and not scene._tcp_ik_missed
            ),
        )
        video.release()
        assert reached, "no reachable height above the floor spot"
        video.pause(1.0)
        video.key(
            Keys.ESCAPE,
            settle=lambda: wait(screen).until(lambda _: not scene._edit_active),
        )
        video.live(1.5)
        video.finish()
        assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)
