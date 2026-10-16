"""Native resize handles on rotated primitives in the actual browser."""

import pytest
import waldoctl
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.scene_screen import (
    gizmo_field,
    EDIT_PROGRAM,
    at_start,
    camera_kept,
    camera_settled,
    clear_selection,
    click_spot,
    confirmed,
    create_shape,
    probe,
    program_previewed,
    set_projection,
)
from tests.helpers.scene_surface import pointer_to, scene_js, wait_for_scene_idle
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state


def _hold_resize(screen, axis=None, symmetric=False):
    spot = wait(screen).until(lambda _: probe(screen, "resizeHandle", axis))
    x, y, dx, dy, axis, sign = spot
    action = ActionChains(screen.selenium, duration=40)
    if symmetric:
        action.key_down(Keys.SHIFT)
    pointer_to(screen, x, y, action)
    action.click_and_hold()
    for k in range(1, 11):
        pointer_to(screen, x + dx * k * 3, y + dy * k * 3, action)
    action.perform()
    # The size change rides on the dragged handle.
    wait(screen).until(
        lambda _: (f := gizmo_field(screen)) and not f["typing"] and f["unit"] == "mm"
    )
    return axis, sign


@pytest.fixture(scope="class")
def resize_screen(class_screen):
    with preserve_capture_buffers(class_screen):
        yield class_screen


@pytest.mark.browser
class TestPrimitiveResize:
    def test_centre_toggle_keeps_both_faces_symmetric_without_shift(
        self, resize_screen
    ):
        screen = resize_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        handle = waldoctl.commander.scene
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM):
            create_shape(screen, "box", "Centred resize")
            initial = handle.shapes[-1]
            click_marked(screen, "object-resize-centre")
            wait(screen).until(
                lambda _: marked_element(screen, "object-resize-centre").get_attribute(
                    "aria-pressed"
                )
                == "true"
            )
            evidence = VisualEvidence(screen, "resize-centre")
            try:
                if evidence.enabled:
                    with camera_kept(screen):
                        click_marked(screen, "object-focus")
                        camera_settled(screen)
                        wait_for_scene_idle(screen)
                        evidence.capture("01-resize-group-centre-toggle-1920")
                    evidence.finish()
                axis, _ = _hold_resize(screen)
                ActionChains(screen.selenium).release().perform()
                confirmed(screen, lambda: handle.shapes[-1] != initial)
                edited = handle.shapes[-1]
                dimension = "xyz"[axis]
                assert edited.pose == pytest.approx(initial.pose)
                assert getattr(edited, dimension) > getattr(initial, dimension)

                spot = wait(screen).until(lambda _: probe(screen, "resizeHandle", axis))
                click_spot(screen, spot)
                wait(screen).until(lambda _: (f := gizmo_field(screen)) and f["typing"])
                ActionChains(screen.selenium).send_keys("25", Keys.ENTER).perform()
                confirmed(
                    screen,
                    lambda: getattr(handle.shapes[-1], dimension)
                    == pytest.approx(getattr(edited, dimension) + 0.025),
                )
                assert handle.shapes[-1].pose == pytest.approx(initial.pose)
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                click_marked(screen, "object-resize-centre")
                wait(screen).until(
                    lambda _: marked_element(
                        screen, "object-resize-centre"
                    ).get_attribute("aria-pressed")
                    == "false"
                )

    def test_rotated_box_face_cancel_center_and_undo(self, resize_screen):
        screen = resize_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        handle = waldoctl.commander.scene
        live = waldoctl.commander.status.joints.angles.rad.copy()
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM):
            evidence = VisualEvidence(
                screen, "primitive-resize/box", synchronized_frames=True
            )
            try:
                create_shape(screen, "box", "Resize fixture")
                initial = handle.shapes[-1]
                evidence.capture("01-face-handles-1920")
                _hold_resize(screen)
                assert handle.shapes[-1] == initial
                evidence.capture("02-face-preview-1920")
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait_for_scene_idle(screen)
                assert handle.shapes[-1] == initial
                _hold_resize(screen)
                ActionChains(screen.selenium).release().perform()
                confirmed(screen, lambda: handle.shapes[-1] != initial)
                edited = handle.shapes[-1]
                evidence.capture("03-face-accepted-1920")
                set_projection(screen, "Orthographic")
                axis, _ = _hold_resize(screen, symmetric=True)
                evidence.capture("04-center-resize-orthographic-1920")
                ActionChains(screen.selenium).release().key_up(Keys.SHIFT).perform()
                confirmed(screen, lambda: handle.shapes[-1] != edited)
                assert handle.shapes[-1].pose == edited.pose
                assert getattr(handle.shapes[-1], "xyz"[axis]) > getattr(
                    edited, "xyz"[axis]
                )
                evidence.stop()
                if evidence.enabled:
                    with viewport(screen, 1366, 768):
                        wait_for_scene_idle(screen)
                        evidence.capture("05-resize-inspector-1366")
                evidence.finish()
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().key_up(
                    Keys.SHIFT
                ).perform()

    def test_cylinder_radius_height_and_sphere_radius(self, resize_screen):
        screen = resize_screen
        screen_wait_for_scene_ready(screen)
        handle = waldoctl.commander.scene
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM):
            evidence = VisualEvidence(
                screen, "primitive-resize/round", synchronized_frames=True
            )
            try:
                for kind in ("cylinder", "sphere"):
                    if run_in_app(lambda: bool(ui_state.urdf_scene._selected_objects)):
                        click_marked(screen, "object-actions")
                        click_marked(screen, "object-hide")
                        # A closing menu would swallow the click on the view.
                        wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
                        clear_selection(screen)
                    create_shape(screen, kind, f"Resize {kind}")
                    for axis in (0, 2) if kind == "cylinder" else (0,):
                        initial = handle.shapes[-1]
                        _hold_resize(screen, axis)
                        evidence.capture(f"{kind}-{axis}-preview-1920")
                        ActionChains(screen.selenium).release().perform()
                        confirmed(screen, lambda: handle.shapes[-1] != initial)
                        edited = handle.shapes[-1]
                        if axis == 0:
                            assert edited.radius > initial.radius
                            assert edited.pose == initial.pose
                        else:
                            assert edited.length > initial.length
                            assert edited.pose != initial.pose
                        evidence.capture(f"{kind}-{axis}-accepted-1920")
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()

    def test_release_before_rejection_restores_confirmed_geometry(
        self, resize_screen, monkeypatch
    ):
        screen = resize_screen
        screen_wait_for_scene_ready(screen)
        at_start(screen)
        view = ui_state.urdf_scene
        with viewport(screen, 1920, 1080):
            create_shape(screen, "box", "Delayed resize")
            original = waldoctl.commander.scene.shapes[-1]
            name = f"shape:{original.name}"
            geometry = scene_js(
                screen,
                "const o=S.byName(arguments[0]);return [o.position.toArray(),o.scale.toArray()]",
                name,
            )
            queued = []
            handle = view.scene.gestures.handle

            def delayed(args):
                if args.get("kind") == "shape":
                    queued.append(args)
                else:
                    handle(args)

            with monkeypatch.context() as patch:
                patch.setattr(view.scene.gestures, "handle", delayed)
                _hold_resize(screen)
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(lambda _: any(a.get("t") == "end" for a in queued))
                wait(screen).until(
                    lambda _: scene_js(
                        screen,
                        "return !!getElement(S.root).core.resizeHandles.confirming",
                    )
                )

                def deliver_stale_events():
                    view.scene.gestures.bump()
                    for args in queued:
                        handle(args)

                run_in_app(deliver_stale_events)
                wait(screen, 5).until(
                    lambda _: scene_js(
                        screen,
                        "const o=S.byName(arguments[0]);return [o.position.toArray(),o.scale.toArray()]",
                        name,
                    )
                    == geometry
                )
                assert waldoctl.commander.scene.shapes[-1] == original
                assert probe(screen, "resizeHandle")

    def test_resize_owns_geometry_during_scene_effects(self, resize_screen):
        screen = resize_screen
        screen_wait_for_scene_ready(screen)
        at_start(screen)
        view = ui_state.urdf_scene
        with viewport(screen, 1920, 1080):
            create_shape(screen, "box", "Animated resize")
            name = "shape:Animated resize"
            node = view._shape_objects[name]
            screen.selenium.execute_cdp_cmd(
                "Emulation.setEmulatedMedia",
                {
                    "features": [
                        {"name": "prefers-reduced-motion", "value": "no-preference"}
                    ]
                },
            )
            try:
                _hold_resize(screen)
                scale = scene_js(
                    screen,
                    "S.requestRender();return S.byName(arguments[0]).scale.toArray()",
                    name,
                )
                first = probe(screen, "frames")
                run_in_app(lambda: view.scene.fx.flash([node]))

                def preview_stays_still(_):
                    assert scene_js(
                        screen,
                        "S.requestRender();return S.byName(arguments[0]).scale.toArray()",
                        name,
                    ) == pytest.approx(scale)
                    return probe(screen, "frames") > first + 30

                wait(screen).until(preview_stays_still)
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait_for_scene_idle(screen)
                assert scene_js(
                    screen,
                    "S.requestRender();return S.byName(arguments[0]).scale.toArray()",
                    name,
                ) == pytest.approx([1, 1, 1])
            finally:
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                screen.selenium.execute_cdp_cmd(
                    "Emulation.setEmulatedMedia",
                    {
                        "features": [
                            {"name": "prefers-reduced-motion", "value": "reduce"}
                        ]
                    },
                )
