"""Pointer-driven object authoring and contextual controls in the running app."""

from contextlib import nullcontext

import pytest
import waldoctl
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.review_scenes import FIXTURE_VIEW, bracket_stl
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    at_start,
    camera_action,
    camera_kept,
    camera_settled,
    choose,
    clear_selection,
    click_object,
    confirmed,
    fill,
    hold,
    object_mode,
    place_shape,
    probe,
    program_previewed,
    settled_element,
)
from tests.helpers.scene_surface import (
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import (
    VisualEvidence,
    enabled,
    preserve_capture_buffers,
)
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.helpers.scene_objects import fixture_stl
from waldo_commander.common.theme import SceneColors
from waldo_commander.services import world_files
from waldo_commander.state import ui_state


def in_scene_panel(screen, marker) -> bool:
    """Whether the element marked ``marker`` shows in the scene panel."""
    try:
        element = marked_element(screen, marker)
        return element.is_displayed() and screen.selenium.execute_script(
            "return !!arguments[0].closest('.scene-panel')", element
        )
    except (NoSuchElementException, StaleElementReferenceException):
        return False


@pytest.fixture(scope="class")
def object_screen(class_screen):
    with preserve_capture_buffers(class_screen):
        yield class_screen


@pytest.mark.browser
class TestObjectEditing:
    def test_imported_fixture_is_visible_at_scaled_size(
        self, object_screen, tmp_path, monkeypatch
    ):
        screen = object_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path / "worlds")
        source = (bracket_stl if enabled() else fixture_stl)(
            tmp_path / "Imported-fixture.stl"
        )
        view = ui_state.urdf_scene
        before = view.fixture_meshes
        with (
            viewport(screen, 1920, 1080) if enabled() else nullcontext(),
            camera_kept(screen) if enabled() else nullcontext(),
        ):
            evidence = VisualEvidence(screen, "stl-import")
            try:
                click_marked(screen, "tab-scene")
                click_marked(screen, "scene-import-mesh")
                upload = settled_element(
                    screen, lambda: marked_element(screen, "mesh-upload")
                )
                upload.find_element(By.CSS_SELECTOR, 'input[type="file"]').send_keys(
                    str(source)
                )
                size = "120 × 80 × 60 mm" if evidence.enabled else "12 × 8 × 4 mm"
                wait(screen).until(
                    lambda _: marked_element(screen, "mesh-size").text == size
                )
                click_marked(screen, "mesh-units-cm")
                if evidence.enabled:
                    wait(screen).until(
                        lambda _: marked_element(screen, "mesh-size").text
                        == "1200 × 800 × 600 mm"
                    )
                    click_marked(screen, "mesh-units-mm")
                    wait(screen).until(
                        lambda _: marked_element(screen, "mesh-size").text == size
                    )
                    scene_js(screen, "S.setCameraPose(arguments[0])", FIXTURE_VIEW)
                    wait_for_scene_idle(screen)
                    evidence.capture("01-importer-size-1920")
                click_marked(screen, "mesh-add")
                confirmed(screen, lambda: len(view.fixture_meshes) == len(before) + 1)
                fixture = run_in_app(lambda: view.fixture_meshes[-1])
                drawn = wait(screen).until(
                    lambda _: scene_js(
                        screen,
                        """
                    const o = S.byName(arguments[0]);
                    const mesh = o?.children.find(c => c.isMesh);
                    if (!mesh) return null;
                    mesh.geometry.computeBoundingBox();
                    const box = mesh.geometry.boundingBox;
                    const size = box.max.clone().sub(box.min).multiply(o.scale);
                    return {visible: o.visible && mesh.visible, size: size.toArray()};
                    """,
                        fixture.key,
                    )
                )
                assert drawn["visible"] and drawn["size"] == pytest.approx(
                    [0.12, 0.08, 0.06] if evidence.enabled else [0.12, 0.08, 0.04]
                )
                wait_for_scene_idle(screen)
                proxy = scene_js(
                    screen,
                    """
                    const o = S.byName(arguments[0]);
                    const edges = o.getObjectByName('fixture:edges');
                    return {
                        fill: o.material.opacity, depthWrite: o.material.depthWrite,
                        edge: edges?.material.opacity,
                        color: edges && '#' + edges.material.color.getHexString(),
                    };
                    """,
                    f"shape:{fixture.proxy_names[0]}",
                )
                assert proxy["fill"] <= 0.1 and not proxy["depthWrite"]
                assert proxy["edge"] >= 0.5 and proxy["color"] == SceneColors.SHAPE_HEX
                if evidence.enabled:
                    camera_action(screen, "focus")
                    camera_settled(screen)
                    markers = [
                        f"scene-mesh-{fixture.name.replace(' ', '-')}",
                        *(
                            f"scene-row-{name.replace(' ', '-')}"
                            for name in fixture.proxy_names
                        ),
                    ]
                    wait(screen).until(
                        lambda _: all(
                            marked_element(screen, marker).is_displayed()
                            for marker in markers
                        )
                    )
                wait_for_scene_idle(screen)
                evidence.capture("02-fixture-group-tree-1920")
                evidence.capture("08-imported-fixture")
                evidence.finish()
            finally:
                evidence.stop()
                if view.fixture_meshes != before:
                    click_marked(screen, "object-undo")
                    confirmed(screen, lambda: view.fixture_meshes == before)
                    wait_for_scene_idle(screen)
                    assert not any(
                        url.startswith("/fixture-assets/")
                        for url in scene_js(screen, "return S.cachedStls()")
                    )

    def test_select_drag_cancel_undo_and_inspector(self, object_screen):
        screen = object_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        handle = waldoctl.commander.scene
        view = ui_state.urdf_scene
        live = waldoctl.commander.status.joints.angles.rad.copy()

        run_in_app(lambda: setattr(handle, "shapes", []))
        confirmed(screen, lambda: not handle.shapes)
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM):
            evidence = VisualEvidence(
                screen, "review-corrections/objects", synchronized_frames=True
            )
            try:
                x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
                place_shape(screen, "box", x, y, "Fixture")
                object_mode(screen, "Resize")
                fill(screen, "object-dim-x", 180)
                confirmed(screen, lambda: handle.shapes[0].x == 0.18)
                fill(screen, "object-dim-y", 130)
                confirmed(screen, lambda: handle.shapes[0].y == 0.13)
                object_mode(screen, "Move")
                clear_selection(screen)
                click_marked(screen, "scene-close")
                wait(screen).until(
                    lambda _: run_in_app(lambda: not ui_state.scene_panel.is_open)
                )
                px, py = scene_object_pixel(screen, "shape:Fixture")
                choose(screen, px, py, "Edit object")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._selected_objects == ["shape:Fixture"]
                    )
                )
                # Selecting the object opens the scene panel on its properties.
                wait(screen).until(lambda _: in_scene_panel(screen, "object-inspector"))
                wait_for_scene_idle(screen)
                plain = scene_js(
                    screen,
                    "const o=S.byName('shape:Fixture'); return "
                    "[o.material.opacity, !!o.getObjectByName('fixture:edges')];",
                )
                assert plain == [0.35, False]
                evidence.capture("01-right-click-inspector-1920")
                initial = handle.shapes[0]
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "shape:Fixture")
                )
                hold(screen, spot, 35, kind="shape")
                evidence.capture("02-translation-preview-1920")
                assert handle.shapes[0] == initial
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait_for_scene_idle(screen)
                assert handle.shapes[0] == initial
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "shape:Fixture")
                )
                hold(screen, spot, 35, kind="shape")
                ActionChains(screen.selenium).release().perform()
                confirmed(screen, lambda: handle.shapes[0].pose != initial.pose)
                evidence.capture("03-move-committed-1920")
                fill(screen, "object-rz", 30)
                confirmed(screen, lambda: handle.shapes[0].pose[5] > 0.5)
                object_mode(screen, "Resize")
                fill(screen, "object-dim-x", 240)
                confirmed(screen, lambda: handle.shapes[0].x == 0.24)
                evidence.capture("04-rotated-resized-1920")
                click_marked(screen, "object-duplicate")
                confirmed(screen, lambda: len(handle.shapes) == 2)
                object_mode(screen, "Move")
                fill(screen, "object-x", 450)
                confirmed(screen, lambda: handle.shapes[-1].pose[0] == 0.45)
                evidence.capture("05-duplicate-1920")
                copy = handle.shapes[-1].name
                click_marked(screen, "object-actions")
                click_marked(screen, "object-hide")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._hidden_objects == {f"shape:{copy}"}
                    )
                )
                wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
                clear_selection(screen)
                assert len(handle.shapes) == 2
                evidence.capture("06-hidden-recovery-1920")
                click_marked(screen, f"scene-eye-{copy}")
                wait(screen).until(
                    lambda _: run_in_app(lambda: not view._hidden_objects)
                )
                click_object(screen, "shape:Fixture")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._selected_objects == ["shape:Fixture"]
                    )
                )
                evidence.stop()
                if evidence.enabled:
                    with viewport(screen, 1366, 768):
                        wait_for_scene_idle(screen)
                        evidence.capture("07-inspector-1366")
                evidence.finish()
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
