"""Review demos: one action per video, slow and smooth, captioned.

Recorded only under ``pytest --evidence`` (conftest leaves this file out
otherwise). Each demo drives the app through its real input paths and
checks the outcome it shows, so a demo of something broken fails instead of
recording it.
"""

import asyncio
import math
import time
from contextlib import contextmanager

import numpy as np
import pytest
import waldoctl
from nicegui import core
from scipy.spatial.transform import Rotation
from waldoctl.shapes import Box
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    focus_editor,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_video import DemoVideo
from tests.helpers.review_scenes import (
    FIXTURE_VIEW,
    SWEEP_VIEW,
    bracket_stl,
    framed_program,
    swept_preview,
    tilted_target_program,
)
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    PATH_PROGRAM,
    PATH_VIEW,
    TCP_VIEW,
    at_start,
    ball_mm,
    camera_action,
    camera_kept,
    camera_pose,
    camera_settled,
    clear_selection,
    click_object,
    confirmed,
    gizmo,
    object_mode,
    path_point,
    place_shape,
    probe,
    program_previewed,
    reset_target_edit,
    settled_element as _settled,
    settled_pixel,
    zoom,
)
from tests.helpers.scene_surface import (
    project_local,
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_par6_backend import par6_env, requires_par6  # noqa: F401
from waldo_commander.services import world_files
from waldo_commander.state import ui_state

# Frame-stepped recording takes a minute or two a demo; demos run only
# under --evidence, outside the suite's time.
pytestmark = pytest.mark.timeout(300)

#: The floor in front of the arm, close enough to see a target land there.
PLACEMENT_VIEW = [0.333, 0.447, 0.402, 0.05, 0.2, 0.15]


def _focus_subject(screen):
    """Fit the selection with room for the handles and their drag travel."""
    camera_action(screen, "focus")
    camera_settled(screen)
    pose = camera_pose(screen)
    zoom(screen, float(np.linalg.norm(np.subtract(pose[:3], pose[3:]))) * 1.2)
    camera_settled(screen)


def _arm_still(timeout: float = 4.0) -> None:
    """Wait until the arm stops moving."""
    deadline = time.monotonic() + timeout
    last = waldoctl.commander.status.joints.angles.rad.copy()
    while time.monotonic() < deadline:
        time.sleep(0.06)
        now = waldoctl.commander.status.joints.angles.rad.copy()
        if np.allclose(now, last, atol=1e-5, rtol=0):
            return
        last = now


def _jog_ended(screen) -> None:
    wait(screen, 15).until(
        lambda _: ui_state.control_panel._jog_end_wait_task is None
        or ui_state.control_panel._jog_end_wait_task.done()
    )
    _arm_still()


@contextmanager
def program_panel(screen):
    """The program editor open beside the scene, so a demo shows its source."""
    click_tab(screen, "program")
    try:
        yield
    finally:
        # The panel slides out under the pointer left on its close button; a
        # button passing under it shows a tooltip that only a move puts away.
        def away() -> None:
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 960, "y": 540}
            )

        away()
        close_panel(screen, "program-panel")
        wait(screen).until(lambda _: no_visible(screen, ".program-panel"))
        away()


@contextmanager
def motion(screen):
    """Animations on, as a user without reduced motion sees them."""

    def emulate(value: str) -> None:
        screen.selenium.execute_cdp_cmd(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-reduced-motion", "value": value}]},
        )

    emulate("no-preference")
    try:
        yield
    finally:
        emulate("reduce")


def _center(element) -> tuple[float, float]:
    r = element.rect
    return r["x"] + r["width"] / 2, r["y"] + r["height"] / 2


def _menu_item(screen, starts: str):
    """The menu entry starting ``starts``, once the menu has finished
    opening, so the pointer is aimed where the entry stays."""
    previous = None

    def settled(_):
        nonlocal previous
        for e in screen.selenium.find_elements(By.CSS_SELECTOR, ".q-menu .q-item"):
            if e.is_displayed() and e.text.strip().startswith(starts):
                rect, previous = (e.rect, previous), e.rect
                moving = screen.selenium.execute_script(
                    "const m=arguments[0].closest('.q-menu');"
                    "return !m || m.getAnimations({subtree:true}).some(a=>a.playState==='running');",
                    e,
                )
                return e if rect[0] == rect[1] and not moving else None
        return None

    return wait(screen).until(settled)


def _outward(
    point: tuple[float, float], away: tuple[float, float], distance: float = 260
) -> tuple[float, float]:
    """A spot ``distance`` px beyond ``point``, on the side away from
    ``away``: an approach from there crosses no link, so no joint's dial
    comes up on the way in."""
    dx, dy = point[0] - away[0], point[1] - away[1]
    n = math.hypot(dx, dy) or 1.0
    return point[0] + dx / n * distance, point[1] + dy / n * distance


def _arm_pixel(screen) -> tuple[float, float]:
    """Where the middle of the arm is on screen."""
    x, y = scene_js(
        screen,
        "const c=getElement(S.root).core,v=new (c.camera.position.constructor)(0,0,0.15);"
        "return c.pointer.screen(v.project(c.camera));",
    )
    return x, y


def _bring_up_gizmo(screen, video: DemoVideo) -> None:
    """From outside the arm to the tool's badge, whose click brings up the
    gizmo, as a user would."""

    def on_top(_):
        badge = screen.selenium.find_element(
            By.CSS_SELECTOR, '.wc-tcp-affordance button[aria-label="Move TCP"]'
        )
        x, y = _center(badge)
        hit = screen.selenium.execute_script(
            "return document.elementFromPoint(arguments[0], arguments[1])"
            "?.getAttribute('aria-label')",
            x,
            y,
        )
        return (x, y) if hit == "Move TCP" else None

    # The badge stands aside while a dial's label would cover it.
    spot = wait(screen).until(on_top)
    video.jump(*_outward(spot, _arm_pixel(screen)))
    video.caption("Click the tool's badge to bring up the gizmo")
    video.pause(0.8)
    video.glide(spot, 1.2)

    def shown() -> None:
        try:
            wait(screen, 5).until(lambda _: gizmo(screen, "tcp:ball"))
        except TimeoutException:
            state = scene_js(
                screen,
                "const c=getElement(S.root).core,h=c.pointer.hover,[x,y]=arguments[0];"
                "const e=document.elementFromPoint(x,y);"
                "return {shown:h.shown,target:h.target,pinned:h.pinned,rules:c.ix.rules,"
                "edit:!!c.ix.edit?.active,at:e&&(e.getAttribute('aria-label')||e.className)};",
                list(spot),
            )
            raise AssertionError(f"no gizmo after the badge click: {state}") from None

    video.click(settle=shown)
    video.pause(0.6)


def _gizmo_center(screen) -> tuple[float, float]:
    x, y = project_local(screen, "tcp:ball", [[0, 0, 0]])[0][:2]
    return x, y


def _press_mark(video, marker, settle=None, seconds=0.45):
    element = _settled(video.screen, lambda: marked_element(video.screen, marker))
    video.glide(_center(element), seconds)
    video.click(settle)


def _open_scene_panel(screen):
    if no_visible(screen, ".scene-panel"):
        click_marked(screen, "tab-scene")
    wait(screen).until(lambda _: marked_element(screen, "scene-tree").is_displayed())


def _frame_choice(video, choice, label):
    screen = video.screen
    badge = _settled(
        screen, lambda: screen.selenium.find_element(By.CSS_SELECTOR, ".wc-gizmo-frame")
    )
    video.jump(*_outward(_center(badge), _arm_pixel(screen), 100))
    video.glide(_center(badge), 0.4)
    video.click()
    _press_mark(video, f"scene-gizmo-{choice}")
    wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
    if label is not None:
        wait(screen).until(lambda _: gizmo(screen, "tcp:ball")["frame"] == label)
    video.frame()


def _edit_target(screen, target):
    from tests.helpers.scene_screen import choose

    choose(screen, *settled_pixel(screen, f"targetgroup:{target}"), "Edit Target...")
    wait(screen).until(lambda _: gizmo(screen, "tcp:ball"))
    wait_for_scene_idle(screen)


def _approach(video, point, seconds=0.5):
    video.jump(*_outward(point, _arm_pixel(video.screen), 100))
    video.glide(point, seconds)


def _drag_arrow(video, name, length=55, seconds=1.8, settle=None):
    screen = video.screen
    x, y, dx, dy = wait(screen).until(lambda _: probe(screen, "arrowTip", name, "X"))
    _approach(video, (x, y))
    video.press()
    video.glide((x + dx * length, y + dy * length), seconds, settle)


def _target_caught_up(screen):
    wait(screen).until(
        lambda _: np.linalg.norm(
            ball_mm(screen)
            - np.asarray(
                run_in_app(lambda: ui_state.urdf_scene._requested_edit_pose[:3])
            )
            * 1000
        )
        < 0.5
    )


@pytest.fixture
def demo_screen(class_screen):
    screen = class_screen
    screen_wait_for_scene_ready(screen, timeout_s=40)
    at_start(screen)
    # Objects an earlier test left would crowd the demo's floor and view.
    run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", []))
    confirmed(screen, lambda: not waldoctl.commander.scene.shapes)
    with viewport(screen, 1920, 1080), camera_kept(screen):
        yield screen
    # A demo that failed keeps what it recorded, to show where it went wrong.
    if DemoVideo.current is not None and DemoVideo.current.frames:
        DemoVideo.current.save("-failed")


@pytest.mark.browser
class TestReviewDemos:
    def test_select_several(self, demo_screen):
        screen = demo_screen
        handle, view = waldoctl.commander.scene, ui_state.urdf_scene
        shapes = [
            Box(name=name, x=0.065, y=0.065, z=0.07, pose=(*point, 0, 0, 0))
            for name, point in (
                ("Fixture A", (0.31, -0.08, 0.035)),
                ("Fixture B", (0.31, 0.08, 0.035)),
            )
        ]
        run_in_app(lambda: setattr(handle, "shapes", shapes))
        confirmed(screen, lambda: handle.shapes == shapes)
        clear_selection(screen)
        _open_scene_panel(screen)
        scene_js(screen, "S.setCameraPose(arguments[0])", FIXTURE_VIEW)
        click_object(screen, "shape:Fixture A")
        click_object(screen, "shape:Fixture B", shift=True)
        _focus_subject(screen)
        clear_selection(screen)
        wait_for_scene_idle(screen)
        video = DemoVideo(screen, "select-several")
        video.jump(1450, 700)
        try:
            video.caption("Select several lets touch taps build a selection")
            _press_mark(
                video,
                "scene-multi-select",
                lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: view._multi_select)
                ),
            )
            video.pause(0.7)
            for count, shape in enumerate(shapes, 1):
                video.tap(
                    tuple(settled_pixel(screen, f"shape:{shape.name}")),
                    settle=lambda count=count: wait(screen).until(
                        lambda _: run_in_app(lambda: len(view._selected_objects))
                        == count
                    ),
                )
                video.pause(0.6)
            video.caption("Drag one arrow to move both keep-outs together")
            _drag_arrow(video, "selection:center", length=85, seconds=2.2)
            video.release(lambda: confirmed(screen, lambda: handle.shapes != shapes))
            delta = np.asarray([s.pose[:3] for s in handle.shapes]) - np.asarray(
                [s.pose[:3] for s in shapes]
            )
            assert np.linalg.norm(delta[0]) > 0.01
            assert delta[0] == pytest.approx(delta[1], abs=1e-6)
            video.pause(1)
            video.caption("Turn Select several off to return to single selection")
            _press_mark(
                video,
                "scene-multi-select",
                lambda: wait(screen).until(
                    lambda _: not run_in_app(lambda: view._multi_select)
                ),
            )
            video.pause(1)
            video.save()
        finally:
            if run_in_app(lambda: view._multi_select):
                click_marked(screen, "scene-multi-select")

    def test_resize_centre(self, demo_screen):
        screen = demo_screen
        handle = waldoctl.commander.scene
        box = Box(
            name="Centred box", x=0.1, y=0.08, z=0.08, pose=(0.3, 0, 0.04, 0, 0, 0)
        )
        run_in_app(lambda: setattr(handle, "shapes", [box]))
        confirmed(screen, lambda: handle.shapes == [box])
        _open_scene_panel(screen)
        click_marked(screen, "scene-row-Centred-box")
        object_mode(screen, "Resize")
        scene_js(screen, "S.setCameraPose(arguments[0])", FIXTURE_VIEW)
        _focus_subject(screen)
        wait_for_scene_idle(screen)
        video = DemoVideo(screen, "resize-centre")
        video.jump(1450, 700)
        video.caption("In Resize, turn on Resize from the centre")
        try:
            _press_mark(
                video,
                "object-resize-centre",
                lambda: wait(screen).until(
                    lambda _: marked_element(
                        screen, "object-resize-centre"
                    ).get_attribute("aria-pressed")
                    == "true"
                ),
            )
            video.pause(1)
            x, y, dx, dy, axis, _ = wait(screen).until(
                lambda _: probe(screen, "resizeHandle", 0)
            )
            video.caption("Pull a face: both opposite faces move equally")
            _approach(video, (x, y))
            video.press()
            video.glide((x + dx * 95, y + dy * 95), 2.5)
            video.release(lambda: confirmed(screen, lambda: handle.shapes[0] != box))
            resized = handle.shapes[0]
            assert resized.pose == pytest.approx(box.pose)
            assert getattr(resized, "xyz"[axis]) > getattr(box, "xyz"[axis])
            video.pause(1.5)
            video.save()
        finally:
            if (
                marked_element(screen, "object-resize-centre").get_attribute(
                    "aria-pressed"
                )
                == "true"
            ):
                click_marked(screen, "object-resize-centre")

    def test_target_tap_line(self, demo_screen):
        screen = demo_screen
        view = ui_state.urdf_scene
        with program_previewed(EDIT_PROGRAM) as targets:
            if not run_in_app(lambda: ui_state.program_panel_visible):
                click_tab(screen, "program")
            focus_editor(screen).send_keys(Keys.CONTROL, Keys.HOME)
            close_panel(screen, "program-panel")
            wait(screen).until(lambda _: no_visible(screen, ".program-panel"))
            scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
            target = targets[-1]
            line = run_in_app(
                lambda: next(
                    t.line_number
                    for t in waldoctl.commander.programs.active.dry_run.targets
                    if t.id == target
                )
            )
            editor_id = run_in_app(lambda: ui_state.active_textarea.id)
            video = DemoVideo(screen, "target-tap-line")
            video.jump(1500, 700)
            video.caption("Tap a target with the program panel closed")
            video.pause(1.3)
            video.tap(tuple(settled_pixel(screen, f"target:{target}")))
            video.live(1)
            wait(screen).until(
                lambda _: run_in_app(lambda: ui_state.program_panel_visible)
            )
            wait(screen).until(
                lambda _: screen.selenium.execute_script(
                    "const v=getElement(arguments[0]).editor;"
                    "return v.state.doc.lineAt(v.state.selection.main.head).number",
                    editor_id,
                )
                == line
            )
            wait(screen).until(
                lambda _: run_in_app(lambda: view._highlighted_line) == line
            )
            video.caption(
                "The program opens at that line; its target and path light up"
            )
            video.frame()
            video.pause(3)
            video.save()
            close_panel(screen, "program-panel")

    def test_gizmo_frames(self, demo_screen, tmp_path):
        screen = demo_screen
        scene = ui_state.urdf_scene
        source = framed_program(tmp_path)
        with program_previewed(source) as targets:
            try:
                scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
                _edit_target(screen, targets[-1])
                initial = np.asarray(run_in_app(lambda: scene._requested_edit_pose))
                tool_x = Rotation.from_euler("XYZ", initial[3:]).as_matrix()[:, 0]
                video = DemoVideo(screen, "gizmo-frames")
                video.caption("The frame badge aligns the target's handles")
                _frame_choice(video, "align-tool", "Tool")
                video.caption("Tool: the X arrow follows the tool's own X axis")
                _drag_arrow(
                    video,
                    "tcp:ball",
                    length=28,
                    seconds=1.6,
                    settle=lambda: _target_caught_up(screen),
                )
                _target_caught_up(screen)
                delta = ball_mm(screen) / 1000 - initial[:3]
                video.release(
                    lambda: wait(screen).until(
                        lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                        != source
                    )
                )
                assert np.linalg.norm(delta) > 0.001
                assert np.linalg.norm(np.cross(delta, tool_x)) < 0.0002
                video.pause(0.7)
                video.caption("Tilted nest: use the setup frame, with its axes shown")
                _frame_choice(video, "align-frame:Tilted-nest", "Tilted-nest")
                wait(screen).until(
                    lambda _: scene_js(
                        screen,
                        "return getElement(S.root).core.gizmos.namedFrame.visible",
                    )
                )
                video.frame()
                video.pause(1.5)
                video.caption("World returns the handles to the world axes")
                _frame_choice(video, "align-world", "World")
                video.pause(1.2)
                video.save()
            finally:
                reset_target_edit(screen)

    def test_keep_vertical(self, demo_screen):
        screen = demo_screen
        source, camera = tilted_target_program()
        scene = ui_state.urdf_scene
        with program_previewed(source) as targets:
            try:
                scene_js(screen, "S.setCameraPose(arguments[0])", camera)
                _edit_target(screen, targets[-1])
                video = DemoVideo(screen, "keep-vertical")
                video.caption(
                    "Keep tool vertical snaps this tilted target to pointing down"
                )
                _frame_choice(video, "vertical", None)
                wait(screen).until(
                    lambda _: gizmo(screen, "tcp:ball")["rotationAxes"] == ["Z"]
                )
                vertical = run_in_app(lambda: ui_state.active_textarea.value)
                assert vertical != source
                pose = np.asarray(run_in_app(lambda: scene._requested_edit_pose))
                assert Rotation.from_euler("XYZ", pose[3:]).as_matrix()[
                    :, 2
                ] == pytest.approx([0, 0, -1], abs=0.01)
                video.frame()
                video.pause(1)
                video.caption(
                    "Only the vertical arc remains: turn it to change heading"
                )
                x, y, dx, dy = wait(screen).until(
                    lambda _: probe(screen, "ringSpot", "Z")
                )
                _approach(video, (x, y))
                video.press()
                video.glide((x + dx * 30, y + dy * 30), 1.8)
                video.release(
                    lambda: wait(screen).until(
                        lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                        != vertical
                    )
                )
                video.pause(0.7)
                video.caption(
                    "Ctrl+Z restores the turn, then the original tilted target"
                )
                video.key(
                    Keys.ESCAPE,
                    lambda: wait(screen).until(
                        lambda _: not run_in_app(lambda: scene._edit_active)
                    ),
                )
                if not run_in_app(lambda: ui_state.program_panel_visible):
                    _press_mark(video, "tab-program")
                editor = _settled(
                    screen,
                    lambda: screen.selenium.find_element(
                        By.CSS_SELECTOR, ".program-panel .cm-content"
                    ),
                )
                video.glide(_center(editor), 0.4)
                video.click()
                for expected in (vertical, source):
                    video.chord(
                        Keys.CONTROL,
                        "z",
                        lambda expected=expected: wait(screen).until(
                            lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                            == expected
                        ),
                    )
                    video.live(0.7)
                video.pause(1)
                video.save()
            finally:
                reset_target_edit(screen)
                if run_in_app(lambda: ui_state.program_panel_visible):
                    close_panel(screen, "program-panel")

    def test_swept_volume(self, demo_screen):
        screen = demo_screen
        with swept_preview(screen) as move:
            scene_js(screen, "S.setCameraPose(arguments[0])", SWEEP_VIEW)
            wait_for_scene_idle(screen)
            video = DemoVideo(screen, "swept-volume")
            video.caption("Right-click the sweeping move, then Show swept volume")
            point = tuple(path_point(screen, move["command"], 0.3)[:2])
            _approach(video, point)
            video.context_click()
            _press_mark(video, "scene-toggle-swept-volume")
            video.live(1)
            sweep = wait(screen).until(
                lambda _: scene_js(screen, "return S.sweptVolume()")
            )
            assert any(s["colliding"] for s in sweep["samples"])
            assert any(not s["colliding"] for s in sweep["samples"])
            video.caption("The colliding part uses the collision colour")
            video.frame()
            video.pause(1.4)
            direction = probe(screen, "viewDirection")
            x, y, _ = wait(screen).until(lambda _: probe(screen, "cubeSpot"))
            video.caption("Orbit to inspect the arm's swept volume")
            video.glide((x, y), 0.5)
            video.press()
            video.glide((x + 35, y + 22), 1.8)
            video.release()
            assert probe(screen, "viewDirection") != direction
            video.pause(0.8)
            video.caption("Right-click the same move to hide its swept volume")
            point = tuple(path_point(screen, move["command"], 0.3)[:2])
            video.glide(point, 0.6)
            video.context_click()
            _press_mark(
                video,
                "scene-toggle-swept-volume",
                lambda: wait(screen).until(
                    lambda _: scene_js(screen, "return S.sweptVolume()") is None
                ),
            )
            video.pause(0.8)
            video.save()

    def test_stl_import(self, demo_screen, tmp_path, monkeypatch):
        screen = demo_screen
        monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path / "worlds")
        source = bracket_stl(tmp_path / "L-bracket.stl")
        handle, view = waldoctl.commander.scene, ui_state.urdf_scene
        original_meshes = view.fixture_meshes
        _open_scene_panel(screen)
        scene_js(screen, "S.setCameraPose(arguments[0])", FIXTURE_VIEW)
        wait_for_scene_idle(screen)
        video = DemoVideo(screen, "stl-import")
        video.jump(1400, 700)
        try:
            video.caption("Import a fixture STL from the scene panel")
            _press_mark(video, "scene-import-mesh")
            upload = _settled(screen, lambda: marked_element(screen, "mesh-upload"))
            video.glide(_center(upload), 0.4)
            upload.find_element(By.CSS_SELECTOR, 'input[type="file"]').send_keys(
                str(source)
            )
            video.frame(
                lambda: wait(screen).until(
                    lambda _: marked_element(screen, "mesh-size").text
                    == "120 × 80 × 60 mm"
                )
            )
            video.pause(0.5)
            video.caption("Units change the imported size: mm → cm → mm")
            for unit, size in (
                ("cm", "1200 × 800 × 600 mm"),
                ("mm", "120 × 80 × 60 mm"),
            ):
                _press_mark(
                    video,
                    f"mesh-units-{unit}",
                    lambda size=size: wait(screen).until(
                        lambda _: marked_element(screen, "mesh-size").text == size
                    ),
                )
                video.pause(0.55)
            video.caption("Place it: the mesh and its keep-out form one fixture")
            _press_mark(
                video,
                "mesh-add",
                lambda: confirmed(
                    screen, lambda: len(view.fixture_meshes) == len(original_meshes) + 1
                ),
            )
            wait(screen).until(
                lambda _: scene_js(
                    screen,
                    "return S.byName('mesh:L-bracket')?.children.some(c=>c.isMesh)",
                )
            )
            clear_selection(screen)
            _press_mark(
                video,
                "scene-mesh-L-bracket",
                lambda: wait(screen).until(
                    lambda _: len(run_in_app(lambda: view._selected_objects)) == 2
                ),
            )
            initial_mesh = view.fixture_meshes[-1]
            initial_box = handle.shapes[-1]
            video.caption("Select the fixture group and move it with one arrow")
            _drag_arrow(video, "selection:center", length=60, seconds=1.3)
            video.release(
                lambda: confirmed(
                    screen, lambda: view.fixture_meshes[-1] != initial_mesh
                )
            )
            mesh_delta = np.subtract(
                view.fixture_meshes[-1].pose[:3], initial_mesh.pose[:3]
            )
            assert np.linalg.norm(mesh_delta) > 0.005
            assert np.subtract(
                handle.shapes[-1].pose[:3], initial_box.pose[:3]
            ) == pytest.approx(mesh_delta)
            video.pause(0.5)
            video.caption("Add a second keep-out box to the group, then resize it")
            _press_mark(
                video,
                "mesh-add-proxy",
                lambda: confirmed(
                    screen, lambda: len(view.fixture_meshes[-1].proxy_names) == 2
                ),
            )
            _press_mark(
                video,
                "object-mode-resize",
                lambda: wait(screen).until(lambda _: probe(screen, "resizeHandle", 1)),
            )
            box = handle.shapes[-1]
            x, y, dx, dy, axis, _ = probe(screen, "resizeHandle", 1)
            _approach(video, (x, y), seconds=0.35)
            video.press()
            video.glide((x - dx * 55, y - dy * 55), 1.4)
            video.release(lambda: confirmed(screen, lambda: handle.shapes[-1] != box))
            assert getattr(handle.shapes[-1], "xyz"[axis]) < getattr(box, "xyz"[axis])
            assert box.name in view.fixture_meshes[-1].proxy_names
            video.pause(1)
            video.save()
        finally:
            if view.fixture_meshes != original_meshes:
                click_marked(screen, "scene-mesh-L-bracket")
                click_marked(screen, "object-actions")
                _settled(
                    screen, lambda: marked_element(screen, "object-delete")
                ).click()
                confirmed(screen, lambda: view.fixture_meshes == original_meshes)

    def test_gizmo_moves_along_an_arrow(self, demo_screen):
        screen = demo_screen
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        pose = waldoctl.commander.status.pose
        before = np.array([pose.x, pose.y, pose.z])
        video = DemoVideo(screen, "gizmo-move-arrow")
        _bring_up_gizmo(screen, video)
        x, y, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
        )
        video.caption("Drag an arrow: the tool moves along that axis")
        video.glide((x, y), 1.0)
        video.pause(0.3)
        video.press()
        video.glide((x + dx * 140, y + dy * 140), 3.0, settle=_arm_still)
        video.caption("The change rides on the arrow as you drag")
        video.pause(1.2)
        video.release(settle=lambda: _jog_ended(screen))
        video.pause(1.0)
        video.save()
        assert np.linalg.norm(np.array([pose.x, pose.y, pose.z]) - before) > 10

    def test_gizmo_moves_in_a_plane(self, demo_screen):
        screen = demo_screen
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        pose = waldoctl.commander.status.pose
        before = np.array([pose.x, pose.y, pose.z])
        video = DemoVideo(screen, "gizmo-move-plane")
        _bring_up_gizmo(screen, video)
        x, y, dx, dy = wait(screen).until(lambda _: probe(screen, "planeSpot", "XY"))
        video.caption("Drag a square: the tool moves in that plane")
        video.glide((x, y), 1.0)
        video.pause(0.3)
        video.press()
        video.along(
            [
                (x, y),
                (x + dx * 90, y + dy * 90),
                (x + dx * 90 - dy * 70, y + dy * 90 + dx * 70),
            ],
            3.5,
            settle=_arm_still,
        )
        video.pause(1.0)
        video.release(settle=lambda: _jog_ended(screen))
        video.pause(1.0)
        video.save()
        after = np.array([pose.x, pose.y, pose.z])
        # The live gizmo's planes are the tool's, not the world's.
        assert np.linalg.norm(after - before) > 10

    def test_gizmo_turns_about_an_arc(self, demo_screen):
        screen = demo_screen
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        pose = waldoctl.commander.status.pose
        before = np.array([pose.rx, pose.ry, pose.rz])
        video = DemoVideo(screen, "gizmo-turn-arc")
        _bring_up_gizmo(screen, video)
        x, y, dx, dy = wait(screen).until(lambda _: probe(screen, "ringSpot", "Z"))
        cx, cy = _gizmo_center(screen)
        radius = math.hypot(x - cx, y - cy)
        start = math.atan2(y - cy, x - cx)
        # Turn the way the ring runs at the press.
        sense = 1 if (-(y - cy) * dx + (x - cx) * dy) > 0 else -1
        arc = [
            (
                cx + radius * math.cos(start + sense * t),
                cy + radius * math.sin(start + sense * t),
            )
            for t in np.linspace(0, math.radians(55), 24)
        ]
        video.caption("Drag an arc: the tool turns about that axis")
        video.glide((x, y), 1.0)
        video.pause(0.3)
        video.press()
        video.along(arc, 3.5, settle=_arm_still)
        video.pause(1.0)
        video.release(settle=lambda: _jog_ended(screen))
        video.pause(1.0)
        video.save()
        # About the tool's own Z, so any of the angles may change.
        turned = (np.array([pose.rx, pose.ry, pose.rz]) - before + 180) % 360 - 180
        assert np.abs(turned).max() > 10

    def test_gizmo_takes_a_typed_value(self, demo_screen):
        screen = demo_screen
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        pose = waldoctl.commander.status.pose
        before = np.array([pose.x, pose.y, pose.z])
        moved = lambda: np.linalg.norm(np.array([pose.x, pose.y, pose.z]) - before)  # noqa: E731
        video = DemoVideo(screen, "gizmo-typed-value")
        _bring_up_gizmo(screen, video)
        x, y, _, _ = wait(screen).until(
            lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
        )
        video.caption("Click an arrow to type an exact move")
        video.glide((x, y), 1.0)
        video.pause(0.3)
        video.click()
        wait(screen).until(
            lambda _: scene_js(
                screen, "return !!getElement(S.root).core.gizmos.precision.entry"
            )
        )
        video.pause(0.6)
        video.type("25")
        video.pause(0.5)
        video.caption("Enter applies it")
        video.key(Keys.ENTER)
        video.live(2.5)
        video.frame(
            settle=lambda: wait(screen, 15).until(
                # Along the tool's own X, which the live gizmo uses.
                lambda _: abs(moved() - 25) < 0.5
            )
        )
        video.pause(1.0)
        video.save()
        assert moved() == pytest.approx(25, abs=0.5)

    def test_editing_a_target_moves_the_target(self, demo_screen):
        screen = demo_screen
        scene = ui_state.urdf_scene
        with program_previewed(EDIT_PROGRAM) as targets, program_panel(screen):
            scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
            wait_for_scene_idle(screen)
            live = waldoctl.commander.status.joints.angles.rad.copy()
            mx, my = scene_object_pixel(screen, f"targetgroup:{targets[-1]}")
            video = DemoVideo(screen, "target-edit")
            video.jump(*_outward((mx, my), _arm_pixel(screen)))
            video.caption("Right-click a target to edit it")
            video.pause(0.8)
            video.glide((mx, my), 1.0)
            video.pause(0.3)
            video.context_click()
            item = _menu_item(screen, "Edit Target")
            video.pause(0.6)
            video.glide(_center(item), 0.7)
            video.click(settle=lambda: wait(screen).until(lambda _: scene._edit_active))
            video.pause(0.8)
            initial = scene._requested_edit_pose
            x, y, dx, dy = wait(screen).until(
                lambda _: probe(screen, "arrowTip", "tcp:ball", "Z")
            )
            video.caption("Drag the gizmo: the target moves, the arm stays put")
            video.glide((x, y), 0.8)
            video.press()
            video.glide((x + dx * 110, y + dy * 110), 3.0)
            video.release(
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose) != initial
                )
            )
            video.caption("Let go and it is kept: its line in the program is rewritten")
            video.pause(2.0)
            video.caption("Ctrl+Z takes it back")
            video.chord(
                Keys.CONTROL,
                "z",
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                    == EDIT_PROGRAM
                ),
            )
            video.live(1.5)
            video.pause(1.0)
            video.save()
            assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    def test_placing_a_target_on_the_floor(self, demo_screen):
        screen = demo_screen
        scene = ui_state.urdf_scene
        with program_previewed(EDIT_PROGRAM), program_panel(screen):
            scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
            wait_for_scene_idle(screen)
            live = waldoctl.commander.status.joints.angles.rad.copy()
            x, y, gx, gy = wait(screen).until(lambda _: probe(screen, "floorSpot"))
            video = DemoVideo(screen, "target-place")
            video.jump(*_outward((x, y), _arm_pixel(screen)))
            video.caption("Over the floor, a ring shows where a target would go")
            video.pause(0.6)
            video.glide((x, y), 2.0)
            video.pause(0.6)
            video.context_click()
            item = _menu_item(screen, "Place Target Here")
            video.pause(0.6)
            video.glide(_center(item), 0.7)
            video.click(settle=lambda: wait(screen).until(lambda _: scene._edit_active))
            video.caption("A new target lands there, in the program at once")
            video.pause(1.5)
            tx, ty, dx, dy = wait(screen).until(
                lambda _: probe(screen, "arrowTip", "tcp:ball", "Z")
            )
            video.caption("Lift it with the Z arrow; red means out of reach")
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
            video.caption("Let go and it is kept; Escape puts the edit away")
            video.pause(1.5)
            video.key(
                Keys.ESCAPE,
                settle=lambda: wait(screen).until(lambda _: not scene._edit_active),
            )
            video.live(2.0)
            video.pause(1.0)
            video.save()
            assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    def test_scrubbing_along_a_path(self, demo_screen):
        screen = demo_screen
        with program_previewed(PATH_PROGRAM):
            scene_js(screen, "S.setCameraPose(arguments[0])", PATH_VIEW)
            wait_for_scene_idle(screen)
            record = scene_js(screen, "return S.inspection().records[0]")
            dry = waldoctl.commander.programs.active.dry_run

            def scrubbed() -> None:
                def matches(_) -> bool:
                    state = scene_js(screen, "return S.inspection()")
                    return bool(state["current"]) and np.allclose(
                        waldoctl.commander.status.joints.angles.rad,
                        dry.commanded.joints_rad[state["current"]["row"]],
                        atol=np.radians(0.05),
                        rtol=0,
                    )

                wait(screen, 10).until(matches)

            # From partway along line 7, clear of the arm, to the path's end.
            first = next(i for i, b in enumerate(record["blocks"]) if b["line"] == 7)
            points = []
            for i, block in enumerate(record["blocks"][first:]):
                pixels = scene_js(
                    screen, "return S.pathPixels(0, arguments[0])", block["command"]
                )
                if not pixels or len(pixels) < 2:
                    continue
                if i == 0:
                    pixels = pixels[len(pixels) // 10 :]
                points += [(p[0], p[1]) for p in pixels[:: max(1, len(pixels) // 40)]]
            video = DemoVideo(screen, "path-scrub")
            video.jump(*_outward(points[0], _arm_pixel(screen)))
            video.caption("Hover a path: it lights up, ready to drag")
            video.pause(0.6)
            video.glide(points[0], 1.0)
            video.pause(0.6)
            video.caption("Press and drag along it: the simulator follows")
            video.press()
            video.along(points, 9.0, settle=scrubbed)
            video.pause(1.0)
            video.release()
            video.pause(1.0)
            video.save()

    def test_joint_dial_turns_a_joint(self, demo_screen):
        screen = demo_screen
        u = 0
        scene_js(
            screen,
            "S.setCameraPose(arguments[0])",
            [0.234, -0.252, 0.37, 0, 0.06, 0.13],
        )
        wait_for_scene_idle(screen)
        before = waldoctl.commander.status.joints.angles.deg[u]
        lx, ly = scene_object_pixel(screen, f"link:L{u + 1}")
        video = DemoVideo(screen, "joint-dial")
        video.jump(*_outward((lx, ly), _arm_pixel(screen), 320))
        video.caption("Hover a link: its joint's dial appears")
        video.pause(0.6)
        video.glide((lx, ly), 1.2)
        try:
            shown = wait(screen).until(
                lambda _: scene_js(
                    screen,
                    "const r=getElement(S.root).core.pointer.ring;return r?[r.u]:null",
                )
            )[0]
        except TimeoutException:
            state = scene_js(
                screen,
                "const c=getElement(S.root).core,p=c.pointer,h=p.hover,[x,y]=arguments[0];"
                "return {shown:h.shown,target:h.target,pinned:h.pinned,grace:h.graceAt,"
                "hovered:p.hovered,pick:S.pick(x,y),rings:Object.keys(c.ix.rings||{})};",
                [lx, ly],
            )
            raise AssertionError(f"no dial on L{u + 1}: {state}") from None
        assert int(shown) == u, f"J{int(shown) + 1}'s dial came up, not J{u + 1}'s"
        dial = wait(screen).until(lambda _: probe(screen, "dial", shown))
        grab = wait(screen).until(lambda _: probe(screen, "ringGrab", shown))
        video.pause(0.8)
        at = dial["q"] + math.radians(grab)
        arc = project_local(
            screen,
            f"jog:dial:{u}",
            [
                [
                    dial["r"] * math.cos(at - math.radians(d)),
                    dial["r"] * math.sin(at - math.radians(d)),
                    0,
                ]
                for d in np.linspace(0, 35, 24)
            ],
        )
        video.caption("Drag around the dial to turn the joint")
        video.glide(tuple(arc[0][:2]), 0.8)
        video.press()
        video.along([tuple(p[:2]) for p in arc], 3.5, settle=_arm_still)
        video.pause(0.8)
        video.release(settle=lambda: _jog_ended(screen))
        video.pause(1.0)
        video.save()
        assert abs(waldoctl.commander.status.joints.angles.deg[u] - before) > 10

    def test_placing_and_moving_a_keepout(self, demo_screen):
        screen = demo_screen
        handle = waldoctl.commander.scene
        run_in_app(lambda: setattr(handle, "shapes", []))
        confirmed(screen, lambda: not handle.shapes)
        scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
        wait_for_scene_idle(screen)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        video = DemoVideo(screen, "keepout-place-move")
        video.jump(*_outward((x, y), _arm_pixel(screen)))
        video.caption("Right-click the floor to add a keep-out there")
        video.pause(0.6)
        video.glide((x, y), 1.0)
        video.context_click()
        submenu = _menu_item(screen, "Create keep-out")
        video.pause(0.5)
        video.glide(_center(submenu), 0.7)
        video.click()
        item = _menu_item(screen, "Box here")
        video.pause(0.4)
        video.glide(_center(item), 0.6)
        video.click(settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 1))
        name = handle.shapes[0].name
        video.caption("It is placed and selected; the scene panel opens with it")
        video.pause(1.8)
        ax, ay, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", f"shape:{name}")
        )
        start = handle.shapes[0].pose
        video.caption("Drag its gizmo to move it")
        video.glide((ax, ay), 0.8)
        video.press()
        video.glide((ax + dx * 120, ay + dy * 120), 3.0)
        video.release(
            settle=lambda: confirmed(screen, lambda: handle.shapes[0].pose != start)
        )
        video.pause(1.5)
        video.save()

    def test_drawing_a_box(self, demo_screen):
        screen = demo_screen
        handle = waldoctl.commander.scene
        run_in_app(lambda: setattr(handle, "shapes", []))
        confirmed(screen, lambda: not handle.shapes)
        scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
        wait_for_scene_idle(screen)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        video = DemoVideo(screen, "keepout-draw-box")
        video.jump(*_outward((x, y), _arm_pixel(screen)))
        video.caption("Right-click the floor, Create keep-out, Draw Box")
        video.pause(0.6)
        video.glide((x, y), 1.0)
        video.context_click()
        submenu = _menu_item(screen, "Create keep-out")
        video.pause(0.5)
        video.glide(_center(submenu), 0.7)
        video.click()
        item = _menu_item(screen, "Draw Box")
        video.pause(0.4)
        video.glide(_center(item), 0.6)
        video.click(
            settle=lambda: wait(screen).until(
                lambda _: probe(screen, "drawing")["active"]
            )
        )
        start, end = probe(screen, "drawingPlane", [[-0.06, -0.05], [0.06, 0.05]])
        video.caption("Drag out its footprint")
        video.glide(tuple(start), 0.8)
        video.press()
        video.glide(tuple(end), 2.5)
        video.release(
            settle=lambda: wait(screen).until(lambda _: probe(screen, "drawingHeight"))
        )
        hx, hy, dx, dy = probe(screen, "drawingHeight")
        video.caption("Then pull up its height")
        video.glide((hx, hy), 0.8)
        video.press()
        video.glide((hx + dx * 110, hy + dy * 110), 2.5)
        video.release(settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 1))
        video.pause(1.5)
        video.save()
        assert handle.shapes[0].z > 0.05

    def test_resizing_a_box(self, demo_screen):
        screen = demo_screen
        handle = waldoctl.commander.scene
        run_in_app(lambda: setattr(handle, "shapes", []))
        confirmed(screen, lambda: not handle.shapes)
        scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
        wait_for_scene_idle(screen)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        place_shape(screen, "box", x, y, "Fixture")
        initial = handle.shapes[0]
        video = DemoVideo(screen, "keepout-resize")
        resize = marked_element(screen, "object-mode-resize")
        video.jump(*_outward(_center(resize), _arm_pixel(screen), 160))
        video.caption("Switch the selected keep-out to Resize in the scene panel")
        video.pause(0.6)
        video.glide(_center(resize), 1.0)
        video.click(
            settle=lambda: wait(screen).until(lambda _: probe(screen, "resizeHandle"))
        )
        hx, hy, dx, dy, *_ = probe(screen, "resizeHandle")
        video.caption("Drag a face: the opposite face stays put")
        video.glide((hx, hy), 0.8)
        video.press()
        video.glide((hx + dx * 90, hy + dy * 90), 3.0)
        video.release(
            settle=lambda: confirmed(screen, lambda: handle.shapes[0] != initial)
        )
        video.pause(1.5)
        video.save()

    def test_isolating_objects(self, demo_screen):
        screen = demo_screen
        view = ui_state.urdf_scene
        handle = waldoctl.commander.scene
        shapes = [
            Box(name=name, x=0.065, y=0.065, z=0.09, pose=(*point, 0, 0, 0))
            for name, point in zip(
                ["Fixture A", "Fixture B", "Clamp", "Stock"],
                [
                    (0.35, -0.18, 0.045),
                    (0.4, 0.05, 0.045),
                    (0.55, -0.1, 0.045),
                    (0.55, 0.15, 0.045),
                ],
            )
        ]
        run_in_app(lambda: setattr(handle, "shapes", shapes))
        confirmed(screen, lambda: handle.shapes == shapes)
        scene_js(
            screen,
            "S.setCameraPose(arguments[0])",
            [0.685, -0.4, 0.295, 0.42, -0.05, 0.05],
        )
        wait_for_scene_idle(screen)
        a = scene_object_pixel(screen, "shape:Fixture A")
        b = scene_object_pixel(screen, "shape:Fixture B")
        video = DemoVideo(screen, "isolate")
        video.jump(*_outward(a, _arm_pixel(screen)))
        video.caption("Hovering a keep-out lights it")
        video.pause(0.6)
        video.glide(a, 1.0)
        video.pause(1.2)
        video.caption("Click it, then shift-click another")
        video.pause(0.6)
        video.click(
            settle=lambda: wait(screen).until(
                lambda _: run_in_app(lambda: len(view._selected_objects)) == 1
            )
        )
        video.pause(0.4)
        video.glide(b, 1.0)
        video.click(
            shift=True,
            settle=lambda: wait(screen).until(
                lambda _: run_in_app(lambda: len(view._selected_objects)) == 2
            ),
        )
        video.pause(0.8)

        def isolate(done) -> None:
            video.glide(_center(marked_element(screen, "object-isolate")), 0.8)
            video.click(settle=lambda: wait(screen).until(lambda _: done()))

        video.caption("Isolate them from the scene panel: the rest fades out")
        isolate(lambda: run_in_app(lambda: view._isolated_view))
        video.pause(2.0)
        video.caption("The same button shows everything again")
        isolate(lambda: not run_in_app(lambda: view._isolated_view))
        video.pause(1.5)
        video.save()

    def test_camera_tools(self, demo_screen):
        screen = demo_screen
        with motion(screen):
            with program_previewed(EDIT_PROGRAM):
                wait_for_scene_idle(screen)
                video = DemoVideo(screen, "camera-tools")
                reset = _center(marked_element(screen, "camera-reset"))
                video.jump(reset[0] - 320, reset[1] + 240)

                def press(caption: str, marker: str, seconds: float = 1.6) -> None:
                    video.caption(caption)
                    video.glide(_center(marked_element(screen, marker)), 0.8)
                    video.pause(0.4)
                    video.click()
                    video.live(seconds)

                press(
                    "The buttons under the cube: Focus frames the robot", "camera-focus"
                )
                press(
                    "Orthographic: no perspective, true proportions",
                    "camera-orthographic",
                    0.8,
                )
                assert scene_js(screen, "return !!S.camera.isOrthographicCamera")
                press("Perspective again", "camera-perspective", 0.8)
                press("Previous view (Shift+F) goes back", "camera-back", 1.4)
                press("Reset view puts the camera back", "camera-reset")
                video.pause(1.0)
                video.save()

    def test_view_cube(self, demo_screen):
        screen = demo_screen

        def spot(direction):
            x, y, _ = wait(screen).until(lambda _: probe(screen, "cubeSpot", direction))
            return x, y

        def facing(direction) -> bool:
            n = np.asarray(direction, float) / np.linalg.norm(direction)
            return float(np.dot(probe(screen, "viewDirection"), n)) > 0.999

        with motion(screen), program_previewed(EDIT_PROGRAM):
            wait_for_scene_idle(screen)
            video = DemoVideo(screen, "view-cube")
            toward = probe(screen, "viewDirection")
            sx, sy = (1 if toward[0] >= 0 else -1), (1 if toward[1] >= 0 else -1)
            edge_direction = [sx, sy, 0]
            edge = spot(edge_direction)
            video.jump(edge[0] - 320, edge[1] + 220)
            video.caption("The view cube, top right, turns with the camera")
            video.pause(0.8)
            video.glide(edge, 1.2)
            video.caption("Its edges are rounded: hover one and its band lights")
            video.pause(1.2)
            video.caption("Click an edge for the view half way between its faces")
            video.click()
            video.live(1.4)
            assert facing(edge_direction)
            corner_direction = [sx, -sy, 1]
            corner = spot(corner_direction)
            video.caption("A corner looks from three faces at once")
            video.glide(corner, 0.9)
            video.pause(0.8)
            video.click()
            video.live(1.4)
            assert facing(corner_direction)
            top = spot([0, 0, 1])
            video.caption("A face looks straight on: here, from the top")
            video.glide(top, 0.8)
            video.pause(0.5)
            video.click()
            video.live(1.2)
            top = spot([0, 0, 1])
            video.caption("Drag the cube to orbit")
            video.glide(top, 0.8)
            before = probe(screen, "viewDirection")
            video.press()
            video.glide((top[0] + 50, top[1] + 60), 2.0)
            video.release()
            assert probe(screen, "viewDirection") != before
            video.pause(0.6)
            video.caption("Reset view, under the cube, puts the camera back")
            video.glide(_center(marked_element(screen, "camera-reset")), 0.8)
            video.click()
            video.live(1.4)
            video.save()

    def test_scene_panel(self, demo_screen):
        screen = demo_screen
        view = ui_state.urdf_scene
        handle = waldoctl.commander.scene
        shapes = [
            Box(name=name, x=0.065, y=0.065, z=0.09, pose=(*point, 0, 0, 0))
            for name, point in (
                ("Fixture", (0.35, -0.18, 0.045)),
                ("Clamp", (0.4, 0.05, 0.045)),
                ("Stock", (0.55, -0.1, 0.045)),
            )
        ]
        run_in_app(lambda: setattr(handle, "shapes", shapes))
        confirmed(screen, lambda: handle.shapes == shapes)
        with program_previewed(EDIT_PROGRAM), program_panel(screen):
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                [0.685, -0.4, 0.295, 0.42, -0.05, 0.05],
            )
            wait_for_scene_idle(screen)
            tab = _center(marked_element(screen, "tab-scene"))
            video = DemoVideo(screen, "scene-panel")
            video.jump(tab[0] + 380, tab[1] - 260)
            video.caption("The scene panel sits by the settings, beside the editor")
            video.pause(0.6)
            video.glide(tab, 1.2)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: marked_element(screen, "scene-tree").is_displayed()
                )
            )
            video.pause(1.0)
            video.caption("It lists everything in the scene; hover a row to find it")
            video.glide(_center(marked_element(screen, "scene-row-Clamp")), 1.0)
            video.pause(1.6)
            video.caption("Click a row to select it and edit its values")
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: view._selected_objects)
                    == ["shape:Clamp"]
                )
            )
            video.pause(0.8)
            field = marked_element(screen, "object-z")
            video.glide(_center(field), 0.9)
            video.click()
            video.chord(Keys.CONTROL, "a")
            video.type("120")
            video.key(
                Keys.ENTER,
                settle=lambda: confirmed(
                    screen,
                    lambda: abs(
                        next(s for s in handle.shapes if s.name == "Clamp").pose[2]
                        - 0.12
                    )
                    < 1e-6,
                ),
            )
            video.pause(1.2)
            video.caption("The eye hides one from view; it is still kept out")
            eye = _center(marked_element(screen, "scene-eye-Stock"))
            video.glide((eye[0] - 120, eye[1]), 0.6)
            video.glide(eye, 0.6)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: "shape:Stock" in view._hidden_objects)
                )
            )
            video.pause(1.4)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(
                        lambda: "shape:Stock" not in view._hidden_objects
                    )
                )
            )
            video.pause(0.6)
            video.caption("+ draws a new keep-out on the floor")
            video.glide(_center(marked_element(screen, "scene-draw")), 0.8)
            video.click()
            item = _settled(
                screen, lambda: marked_element(screen, "scene-panel-draw-box")
            )
            video.pause(0.3)
            video.glide(_center(item), 0.6)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: probe(screen, "drawing")["active"]
                )
            )
            start, end = probe(screen, "drawingPlane", [[0.3, 0.2], [0.38, 0.28]])
            video.glide(tuple(start), 0.8)
            video.press()
            video.glide(tuple(end), 2.0)
            video.release(
                settle=lambda: wait(screen).until(
                    lambda _: probe(screen, "drawingHeight")
                )
            )
            hx, hy, dx, dy = probe(screen, "drawingHeight")
            video.glide((hx, hy), 0.7)
            video.press()
            video.glide((hx + dx * 90, hy + dy * 90), 2.0)
            video.release(
                settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 4)
            )
            video.caption("It joins the list, selected")
            video.pause(2.0)
            video.save()

    def test_workcell_library(self, demo_screen, tmp_path, monkeypatch):
        screen = demo_screen
        monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path)
        handle = waldoctl.commander.scene
        shapes = [
            Box(name=name, x=0.065, y=0.065, z=0.09, pose=(*point, 0, 0, 0))
            for name, point in (
                ("Fixture", (0.35, -0.18, 0.045)),
                ("Stock", (0.4, 0.05, 0.045)),
            )
        ]
        run_in_app(lambda: setattr(handle, "shapes", shapes))
        confirmed(screen, lambda: handle.shapes == shapes)
        view = ui_state.urdf_scene
        with program_previewed(EDIT_PROGRAM), program_panel(screen):
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                [0.685, -0.4, 0.295, 0.42, -0.05, 0.05],
            )
            wait_for_scene_idle(screen)
            floor = wait(screen).until(lambda _: probe(screen, "floorSpot"))[:2]
            video = DemoVideo(screen, "workcell-library")
            video.jump(floor[0] + 200, floor[1] + 120)
            video.caption("Right-click the floor, Workcell library…")
            video.glide(floor, 1.0)
            video.context_click()
            item = _menu_item(screen, "Workcell library")
            video.pause(0.4)
            video.glide(_center(item), 0.6)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: marked_element(screen, "workcell-library").is_displayed()
                )
            )
            video.caption("It opens in the scene panel, beside the program")
            video.pause(1.2)
            video.caption("Name the objects and save them")
            video.glide(_center(marked_element(screen, "workcell-name")), 0.8)
            video.click()
            video.type("bench")
            video.glide(_center(marked_element(screen, "workcell-save")), 0.6)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: "Saved bench"
                    in marked_element(screen, "workcell-feedback").text
                )
            )
            video.pause(1.2)
            video.caption("Delete one of them")
            video.glide(scene_object_pixel(screen, "shape:Stock"), 1.0)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: view._selected_objects)
                    == ["shape:Stock"]
                )
            )
            video.glide(_center(marked_element(screen, "object-actions")), 0.8)
            video.click()
            delete = _settled(screen, lambda: marked_element(screen, "object-delete"))
            video.pause(0.3)
            video.glide(_center(delete), 0.6)
            video.click(
                settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 1)
            )
            video.pause(0.8)
            video.caption("Load bench puts the saved objects back")
            video.glide(_center(marked_element(screen, "workcell-load:bench")), 1.0)
            video.click(
                settle=lambda: confirmed(screen, lambda: handle.shapes == shapes)
            )
            video.pause(1.4)
            video.caption("Or insert them into the program as code")
            video.glide(_center(marked_element(screen, "workcell-insert")), 0.8)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: "set_shapes("
                    in run_in_app(lambda: ui_state.active_textarea.value)
                )
            )
            video.pause(2.5)
            video.save()
            marked_element(screen, "workcell-close").click()


@requires_par6
@pytest.mark.browser
@pytest.mark.usefixtures("par6_env")
def test_comparing_plan_and_prediction(screen):
    from par6 import config

    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=60)
    assert ui_state.active_robot.backend_package == "par6"
    start = np.degrees(config.homing_ready_pose_rad()).tolist()
    asyncio.run_coroutine_threadsafe(
        ui_state.control_panel.client.teleport(start), core.loop
    ).result(10)
    waypoints = []
    for changes in (
        (-60, -15, 10),
        (60, -15, 10),
        (60, 15, -10),
        (-60, 15, -10),
        (0, 0, 0),
    ):
        target = np.array(start, dtype=float)
        target[:3] = [v + delta for v, delta in zip(start[:3], changes)]
        waypoints.append(np.round(target, 2).tolist())
    # Full speed and acceleration: the arm lags the plan by tens of mm.
    source = (
        "from par6 import RobotClient\nrbt = RobotClient()\n"
        + "\n".join(f"rbt.move_j({t}, speed=1.0, accel=1.0)" for t in waypoints)
        + "\nrbt.delay(0.5)\n"
    )
    with viewport(screen, 1920, 1080), camera_kept(screen), program_previewed(source):
        scene_js(
            screen,
            "S.setCameraPose(arguments[0])",
            [0.112, -0.192, 0.474, 0, 0.2, 0.25],
        )
        peak = wait(screen, 30).until(
            lambda _: (
                b := screen.selenium.find_element(
                    By.XPATH, "//button[normalize-space()='Peak']"
                )
            ).is_enabled()
            and b
        )
        wait_for_scene_idle(screen)
        video = DemoVideo(screen, "comparison")
        video.jump(1500, 260)
        video.caption("The plan, with the physics prediction drawn where it leaves it")
        video.pause(2.5)
        video.caption("Peak jumps to the largest deviation")
        video.glide(_center(peak), 1.2)
        video.click(
            settle=lambda: wait(screen).until(
                lambda _: scene_js(
                    screen, "return S.byName('comparison:ghost')?.visible"
                )
            )
        )
        video.caption(
            "The ghost is where physics puts the arm; the card says how far off"
        )
        video.pause(3.0)
        record = scene_js(screen, "return S.inspection().records[0]")
        points = []
        for block in record["blocks"]:
            pixels = scene_js(
                screen, "return S.pathPixels(0, arguments[0])", block["command"]
            )
            if pixels and len(pixels) > 1:
                points += [(p[0], p[1]) for p in pixels[:: max(1, len(pixels) // 30)]]
        mid = points[len(points) // 4 :]
        video.caption("Drag along the path to compare them anywhere")
        video.glide(mid[0], 1.0)
        video.press()
        video.along(mid[: len(mid) // 2], 7.0)
        video.release()
        video.pause(1.5)
        video.save()
