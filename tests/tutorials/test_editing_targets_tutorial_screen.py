"""Tutorial: editing a program's targets in the 3D view."""

import pytest
import waldoctl
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import run_in_app
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    object_box,
    PLACEMENT_VIEW,
    arm_pixel,
    camera_to,
    choose_item,
    menu_item,
    outward,
    program_panel,
    target_caught_up,
    text_rect,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.scene_screen import EDIT_PROGRAM, TCP_VIEW, probe, program_previewed
from tests.helpers.scene_surface import (
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.video_compose import Rect
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: The program's code, top left, at twice the size: wide enough for a
#: target's line.
CODE = Rect(0, 0, 960, 540)
#: Where the program panel ends and the scene begins.
PANEL_RIGHT = 760
#: Where the pointer goes out of the way: empty sky, off the floor, whose
#: hover ring would show where it rests.
AWAY = (1000, 150)


def _around(x: float, y: float, w: float = 1200, h: float = 675) -> Rect:
    """A shot of the scene round (x, y), low in it: a target dragged or
    lifted goes up, clear of the caption at the top. It starts right of
    the program panel, a sliver of which would show at its edge."""
    return Rect(max(x - w / 2, PANEL_RIGHT), y - h * 0.65, w, h)


def test_editing_targets(demo_screen):
    screen = demo_screen
    scene = ui_state.urdf_scene

    def source() -> str:
        return run_in_app(lambda: ui_state.active_textarea.value)

    def changed_line(before: str):
        """The editor's line where the program first differs from ``before``."""
        old, new = before.splitlines(), source().splitlines()
        index = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), len(old))
        return screen.selenium.find_elements(
            By.CSS_SELECTOR, ".program-panel .cm-line"
        )[index]

    def editing() -> None:
        wait(screen).until(lambda _: scene._edit_active)

    def done() -> None:
        wait(screen).until(lambda _: not scene._edit_active)

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
        video.focus(_around(mx, my), margin=0)

        video.step(
            "Each move's target shows in the view",
            mark=object_box(screen, f"targetgroup:{targets[-1]}"),
        )
        video.glide((mx, my), 1.0)
        video.highlight(None)
        video.step("Right-click it and choose Edit Target")
        video.context_click()
        item = menu_item(screen, "Edit Target")
        choose_item(video, item)
        video.click(settle=editing)
        video.pause(OUTCOME_S)

        initial = scene._requested_edit_pose
        x, y, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", "tcp:ball", "Z")
        )
        video.step("Drag the gizmo; the arm stays put")
        # Along the arrow from beyond its tip: across the gizmo, its other
        # handles would light up.
        video.jump(x + dx * 140, y + dy * 140)
        video.glide((x, y), 0.6)
        video.press()
        video.glide(
            (x + dx * 110, y + dy * 110), 2.0, settle=lambda: target_caught_up(screen)
        )
        video.release(
            settle=lambda: wait(screen).until(
                lambda _: run_in_app(lambda: scene._requested_edit_pose) != initial
            )
        )
        wait(screen).until(lambda _: source() != EDIT_PROGRAM)
        video.pause(OUTCOME_S)
        # Before the undo: during an edit the target holds the dragged pose
        # until the edit ends, whatever the code says.
        video.step("Escape finishes editing", keys=[["Esc"]])
        video.key(Keys.ESCAPE, settle=done)
        video.keys()
        video.pause(OUTCOME_S)

        video.hide_pointer()
        video.jump(*AWAY)
        video.focus(CODE, margin=0, caption="bottom")
        edited = changed_line(EDIT_PROGRAM)
        video.step("Its line in the program is rewritten")
        video.underline(text_rect(screen, edited))
        video.pause(CHANGE_S)

        video.step("Ctrl+Z puts it back", keys=[["Ctrl", "Z"]])
        video.chord(
            Keys.CONTROL,
            "z",
            settle=lambda: wait(screen).until(lambda _: source() == EDIT_PROGRAM),
        )
        video.pause(CHANGE_S)
        video.highlight(None)
        video.keys()
        video.caption(None)
        video.focus(None)
        camera_to(video, PLACEMENT_VIEW, factor=5)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        video.focus(_around(x, y), margin=0)
        video.jump(*outward((x, y), arm_pixel(screen)))
        video.step("Right-click the floor: Place Target Here")
        video.glide((x, y), 1.0)
        video.context_click()
        item = menu_item(screen, "Place Target Here")
        choose_item(video, item)
        before = source()
        video.click(settle=editing)
        wait(screen).until(
            lambda _: source().count("move_") > EDIT_PROGRAM.count("move_")
        )
        video.pause(OUTCOME_S)

        video.hide_pointer()
        video.focus(CODE, margin=0, caption="bottom")
        video.step("A move to it joins the program")
        video.underline(text_rect(screen, changed_line(before)))
        video.pause(CHANGE_S)

        video.highlight(None)
        tx, ty, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", "tcp:ball", "Z")
        )
        video.focus(_around(tx, ty), margin=0)
        video.step("Lift it off the floor")
        # Along the arrow from beyond its tip, past none of the other handles.
        video.jump(tx + dx * 140, ty + dy * 140)
        video.glide((tx, ty), 0.6)
        video.press()
        reached = video.along(
            [(tx, ty), (tx + dx * 260, ty + dy * 260)],
            4.0,
            settle=lambda: target_caught_up(screen),
            until=lambda: run_in_app(
                lambda: scene._requested_edit_pose[2] > 0.04
                and not scene._tcp_ik_missed
            ),
        )
        video.release()
        assert reached, "no reachable height above the floor spot"
        video.pause(OUTCOME_S)
        lifted = source()
        video.step("Escape finishes editing", keys=[["Esc"]])
        video.key(Keys.ESCAPE, settle=done)
        video.keys()
        video.hide_pointer()
        video.jump(*AWAY)
        video.focus(CODE, margin=0, caption="bottom")
        video.step("Its line now has the new height")
        video.underline(text_rect(screen, changed_line(before)))
        assert source() == lifted
        video.pause(CHANGE_S)
        video.finish()
        assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)
