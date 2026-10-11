"""The 3D scene in a real browser: the jog handles under real pointer events,
the render loop, the fog, the context menu, a held object's world pose, the
physics overlay of a predicted run, and the view outliving its GPU context.

The tests share one page through ``class_screen``. Each puts back the camera,
shapes, appearance mode and window size it changes. The gizmo test checks the
gizmo is absent until TCP is hovered, so it runs first; the physics overlay
leaves the program column open, so it runs late, and losing the GPU context
runs last.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING

import numpy as np
import pytest
import waldoctl
from nicegui import core
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.mouse_button import MouseButton
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from waldoctl import Box

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    defocus_editor,
    focus_editor,
    js,
    run_in_app,
    send_global_key,
    tap,
)
from tests.helpers.browser_session import no_visible, wait, window_size
from tests.helpers.scene_screen import (
    camera_kept,
    camera_pose,
    camera_settled,
    choose,
    clear_selection,
    settled_pixel,
    end_target_edit,
    frame_mm,
    gizmo,
    grab_ring,
    joint_deg,
    joint_settled,
    poll,
    probe,
    program_previewed,
    tcp_mm,
    teleport_jog_pose,
    zoom,
)
from tests.helpers.scene_surface import (
    emits_during,
    frames_at_rest,
    hover_scene_object,
    pointer_to,
    project_local,
    scene_canvas,
    scene_js,
    scene_object,
    scene_object_pixel,
    shown_handles,
    wait_for_scene_idle,
    snap_deg,
    timed_emits_during,
)
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_scene3d_screen import _touch
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
from waldo_commander.constants import DEFAULT_CAMERA
from waldo_commander.services.keybindings import keybindings_manager
from waldo_commander.services.motion_guard import motion_guard
from waldo_commander.services.programs import preview_is_current
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

#: How long a shown handle outlasts the pointer leaving it (``pointer.js``).
_HOVER_GRACE_S = 0.2


_MENU_ITEMS = """
return [...document.querySelectorAll('.q-menu')]
  .filter((m) => m.getClientRects().length > 0)
  .flatMap((m) => [...m.querySelectorAll('.q-item')].map((i) => i.textContent.trim()));
"""

# A program with two targets, the last of them a move_l in front of the arm.
_TARGET_PROGRAM = (
    "from parol6 import RobotClient\n"
    "rbt = RobotClient()\n"
    "rbt.move_j([85.000, -85.000, 175.000, 5.000, 5.000, 175.000], speed=0.5)\n"
    "rbt.move_l([0.000, 280.000, 250.000, 90.000, 0.000, 90.000], speed=0.5)\n"
)

_HELD_MATRIX = """
const o = S.byName('shape:held-part');
if (!o) return null;
o.updateWorldMatrix(true, false);
return o.matrixWorld.elements;
"""

# Walks the three.js scene for the physics overlay group and reports what is
# in it: the predicted polyline (vertex-coloured, so the following-error
# gradient is real geometry and not a uniform), the contact arrows and the COM.
_OVERLAY_JS = """
const group = S.byName('simulation:physics');
if (!group) return {found: false};
let meshes = 0, shown = 0;
group.traverse((o) => {
  if (o !== group && o.isMesh) {
    meshes += 1;
    if (o.visible) shown += 1;
  }
});
return {found: true, meshes, shown};
"""

_VIEW_FLAGS = (
    "paths_visible",
    "predicted_visible",
    "contacts_visible",
    "com_visible",
)


def _preview_settled() -> bool:
    """Whether the active program's preview answers its source with no new
    run pending."""
    from waldo_commander.components.simulation_engine import simulation

    tab = waldoctl.commander.programs.active
    return (
        tab is not None
        and preview_is_current(tab)
        and simulation._simulation_debounce_timer is None
        and simulation._physics_timer is None
    )


def _caught_up() -> None:
    """Return once every event the browser has sent so far has reached the
    app, and every update the app sent in answer has reached the browser:
    each round trip's reply comes back behind what was sent before it."""
    client = ui_state.urdf_scene.scene.client

    async def round_trips() -> None:
        await client.run_javascript("0", timeout=5.0)
        await client.run_javascript("0", timeout=5.0)

    assert core.loop is not None
    asyncio.run_coroutine_threadsafe(round_trips(), core.loop).result(15)


def _drawn_at_fk(screen: Screen, urdf, link: str, q: list[float]) -> bool:
    """Whether ``link:<link>`` is drawn where the URDF's FK puts it at *q*."""
    model = urdf.urdf_model
    visual = next(v for v in model.link_map[link].visuals)
    expected = model.link_fk(cfg=dict(zip(urdf.joint_names, q)), link=link)
    expected = expected @ visual.origin
    drawn = scene_js(
        screen,
        "const o = S.byName(arguments[0]);"
        "o.updateWorldMatrix(true, false);"
        "return o.matrixWorld.elements;",
        f"link:{link}",
    )
    world = np.asarray(drawn).reshape((4, 4), order="F")
    turned = world[:3, :3] / np.linalg.norm(world[:3, :3], axis=0)
    return bool(
        np.allclose(world[:3, 3], expected[:3, 3], atol=1e-6)
        and np.allclose(turned, expected[:3, :3], atol=1e-6)
    )


def _drag_along(
    screen: Screen, x: float, y: float, dx: float, dy: float, length: float = 40
) -> None:
    """A left-button drag from (x, y) along the unit direction (dx, dy)."""
    actions = ActionChains(screen.selenium, duration=20)
    pointer_to(screen, x, y, actions)
    actions.click_and_hold()
    for step in range(1, 9):
        pointer_to(
            screen, x + dx * length * step / 8, y + dy * length * step / 8, actions
        )
    actions.release()
    actions.perform()


def _records(
    q_rad: np.ndarray, rows: int = 40
) -> tuple[waldoctl.TickIndex, waldoctl.TickIndex]:
    """A commanded/predicted pair the way a backend reports them: a TCP
    path that sweeps forward, a lag that grows along it, and contact for
    the middle third.

    The predicted joints hold at *q_rad* on every row on purpose. Playback
    teleports the simulated arm to whatever the record says, so a record
    that moved it would leave it moved for every test after this one — and
    a pose the planner will not accept turns into a self-collision refusal
    three tests later. What this checks is the drawing, not the arm.
    """
    t = np.linspace(0.0, 1.0, rows, dtype=np.float32)
    joints = np.tile(np.asarray(q_rad, dtype=np.float32)[:6], (rows, 1))
    commanded = joints + t[:, None] * 0.02
    tcp = np.zeros((rows, 6), dtype=np.float32)
    tcp[:, 0] = 0.25 + t * 0.12
    tcp[:, 2] = 0.30 - t * 0.06

    lo, hi = rows // 3, 2 * rows // 3
    starts = np.zeros(rows + 1, dtype=np.uint32)
    pos, force = [], []
    for r in range(rows):
        if lo <= r < hi:
            pos.append([tcp[r, 0], 0.02, tcp[r, 2] - 0.03])
            force.append([0.0, -6.0, 14.0])
        starts[r + 1] = len(pos)
    com = np.zeros((rows, 3), dtype=np.float32)
    com[:, 0] = 0.10 + t * 0.02
    com[:, 2] = 0.22

    blocks = (waldoctl.TickBlock(command=0, start_row=0, rows=rows, line_number=1),)
    tool_closed = np.linspace(0.0, 1.0, rows, dtype=np.float32)
    return (
        waldoctl.TickIndex(
            row_dt_s=0.02,
            joints_rad=commanded.astype(np.float32),
            tcp=tcp,
            tool_closed=tool_closed,
            tool_gripping=np.zeros(rows, dtype=np.bool_),
            blocks=blocks,
            digest=b"screen-commanded",
        ),
        waldoctl.TickIndex(
            row_dt_s=0.02,
            joints_rad=joints.astype(np.float32),
            tcp=tcp,
            tool_closed=tool_closed,
            tool_gripping=np.zeros(rows, dtype=np.bool_),
            blocks=blocks,
            objects=(),
            digest=b"screen-predicted",
            channels={
                "com": com,
                "contact_pos": np.asarray(pos, dtype=np.float32).reshape(-1, 3),
                "contact_force": np.asarray(force, dtype=np.float32).reshape(-1, 3),
                "contact_starts": starts,
            },
        ),
    )


def _tcp_badge(screen):
    """The shown 'Move TCP' badge, which is what reveals the gizmo, or None."""
    return next(
        (
            b
            for b in screen.selenium.find_elements(
                By.CSS_SELECTOR, '.wc-tcp-affordance button[aria-label="Move TCP"]'
            )
            if b.is_displayed()
        ),
        None,
    )


def _movement_handles(screen, shown: bool) -> None:
    """Switch Settings › View › Movement handles to ``shown``."""
    click_marked(screen, "tab-settings")
    click_marked(screen, "settings-cat-view")
    click_marked(screen, "gizmo-visibility")
    poll(
        lambda: waldoctl.commander.settings.view.gizmo_visible == shown,
        5.0,
        f"movement handles {'shown' if shown else 'hidden'}",
    )
    click_marked(screen, "settings-close")
    wait(screen).until(lambda _: no_visible(screen, ".q-dialog"))


def _hover_tcp(screen):
    # The badge follows the camera on the next frame drawn.
    wait_for_scene_idle(screen)
    button = wait(screen).until(lambda _: _tcp_badge(screen))
    ActionChains(screen.selenium).move_to_element(button).perform()


@pytest.mark.browser
class TestScene:
    def test_gizmo_snap_follows_the_zoom_without_reattaching(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        teleport_jog_pose()
        with camera_kept(screen):
            zoom(screen, 1.3)
            _hover_tcp(screen)
            far = poll(lambda: gizmo(screen, "tcp:ball", True), 5, "the gizmo")
            assert far["translationSnap"] > 0 and far["rotationSnap"] > 0
            zoom(screen, 0.4)
            near = poll(
                lambda: (s := gizmo(screen, "tcp:ball"))
                and s["translationSnap"] < far["translationSnap"]
                and s["rotationSnap"] < far["rotationSnap"]
                and s,
                5,
                "finer translation and rotation increments",
            )
            assert near["probe"], "zoom replaced the gizmo"

    def test_ring_drag_moves_the_joint_by_whole_steps_and_leaves_the_camera(
        self, class_screen: Screen
    ) -> None:
        """A ring drag turns its joint by whole steps of the zoom's snap, as far
        as the pointer goes around the ring, on it or well off it, and is let
        go of past the canvas; it never orbits the camera, and it tells the
        app only each new step, with keep-alives while the pointer rests."""
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        teleport_jog_pose()

        with camera_kept(screen):
            zoom(screen, 1.3)

            dial, grab = grab_ring(screen, "link:L1", 0)
            step = snap_deg(screen)
            label = scene_js(screen, "return S.label('jog:dial:0:label')")
            assert label and "step" not in label and "Δ" not in label, label
            start = joint_settled(0)
            camera = camera_pose(screen)

            # Let go over something covering the canvas, 18° to 35° round
            # the ring from the grab, the way the joint has room to turn.
            q, lo, hi = dial["q"], dial["lo"], dial["hi"]
            grabbed = q + math.radians(grab)

            def turn(spot: list[float]) -> float:
                return (spot[2] - grabbed + math.pi) % (2 * math.pi) - math.pi

            spots = [
                spot
                for spot in probe(screen, "coveredSpots", 0) or []
                if math.radians(18) <= abs(turn(spot)) <= math.radians(35)
                and (lo is None or lo <= q + turn(spot) <= hi)
            ]
            assert spots, "nothing covers the canvas where the ring can be let go"
            outside = min(spots, key=lambda spot: abs(turn(spot)))
            sweep = turn(outside)

            # Sweep there, half along the ring and half at 1.6 times its
            # radius, where the held pointer keeps turning it; then out to the
            # covered pixel at the same angle.
            angles = [grabbed + sweep * k / 10 for k in range(11)]
            points = [
                [r * math.cos(t), r * math.sin(t), 0.0]
                for r, t in (
                    (dial["r"] * (1.0 if k <= 5 else 1.6), t)
                    for k, t in enumerate(angles)
                )
            ]
            path = project_local(screen, "jog:dial:0", points)
            assert path is not None

            def drag() -> None:
                # One chain, so the button stays down through the pause.
                actions = ActionChains(screen.selenium, duration=20)
                pointer_to(screen, path[0][0], path[0][1], actions)
                actions.click_and_hold()
                for x, y in path[1:]:
                    pointer_to(screen, x, y, actions)
                actions.pause(0.35)
                pointer_to(screen, outside[0], outside[1], actions)
                actions.release()
                actions.perform()
                _caught_up()

            sent = emits_during(screen, drag)
            poll(lambda: abs(joint_deg(0) - start) > 4.0, 15.0, "J1 to move")
            moved = abs(joint_settled(0) - start)
            steps = moved / step
            assert moved >= 15.0 and abs(steps - round(steps)) < 0.02, (
                f"J1 moved {moved:.3f}°, not 15° or more in whole {step}° steps"
            )
            assert camera_pose(screen) == pytest.approx(camera, abs=1e-9), (
                "the drag orbited the camera"
            )
            # One begin, a move per new step, keep-alives, and one release.
            assert sent[0] == "begin" and sent[-1] == "end", sent
            assert set(sent[1:-1]) <= {"move", "keep"}, sent
            assert "keep" in sent, sent
            assert sent.count("move") <= round(moved / step) + 2, sent

    def test_the_view_cube_draws_over_the_scene_without_wiping_it(
        self, class_screen: Screen
    ) -> None:
        """The cube is drawn after the scene, into its own corner: the scene
        stays drawn around it and the cube shows over what is behind it."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        pixels = """
            const link = S.hoverPixel('link:L3');
            const box = document.querySelector('.wc-viewcube')?.getBoundingClientRect();
            if (!link || !box) return null;
            const cube = [box.left + box.width / 2, box.top + box.height / 2];
            const gl = S.renderer.getContext();
            const r = S.canvas.getBoundingClientRect();
            const dpr = S.renderer.getPixelRatio();
            const read = (px, py) => { const out = new Uint8Array(4);
              gl.readPixels(Math.round((px - r.left) * dpr), Math.round((r.bottom - py) * dpr),
                            1, 1, gl.RGBA, gl.UNSIGNED_BYTE, out);
              return Array.from(out).slice(0, 3); };
            S.renderer.render(S.scene, S.camera);
            const scene = {link: read(...link), cube: read(...cube)};
            S.drawFrame();
            return {scene, frame: {link: read(...link), cube: read(...cube)}};
        """
        drawn = poll(lambda: scene_js(screen, pixels), 10.0, "L3 and the cube")
        assert drawn["frame"]["link"] == pytest.approx(drawn["scene"]["link"], abs=6), (
            "drawing the cube wiped the scene"
        )
        assert (
            np.abs(np.subtract(drawn["frame"]["cube"], drawn["scene"]["cube"])).sum()
            > 12
        ), "the cube is not drawn over the scene"

    def test_the_view_cube_turns_and_orbits_the_camera(
        self, class_screen: Screen
    ) -> None:
        """A click on one of the cube's corners, edges or faces turns the
        camera to look from there; a drag on it orbits the camera about its
        target, and letting go of the drag turns it no further."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)

        def click(x: float, y: float) -> None:
            actions = ActionChains(screen.selenium, duration=0)
            pointer_to(screen, x, y, actions)
            actions.click()
            actions.perform()

        def looks_from(direction: list[float]) -> bool:
            return np.dot(probe(screen, "viewDirection"), direction) > 0.999

        def turn_to(wanted: list[int] | None, what: str) -> list[float]:
            x, y, direction = poll(
                lambda: probe(screen, "cubeSpot", wanted), 5.0, f"{what} on the cube"
            )
            click(x, y)
            poll(lambda: looks_from(direction), 5.0, f"the camera to look from {what}")
            camera_settled(screen)
            return direction

        with camera_kept(screen):
            corner = [1 if v >= 0 else -1 for v in probe(screen, "viewDirection")]
            turn_to(corner, "the corner nearest the viewer")
            turn_to([corner[0], corner[1], 0], "the upright edge nearest the viewer")
            face = turn_to(None, "a side face")

            before = camera_pose(screen)
            x, y, _ = poll(
                lambda: probe(screen, "cubeSpot", [round(v) for v in face]),
                5.0,
                "the face looked at",
            )
            _drag_along(screen, x, y, 1, 0, 40)
            camera_settled(screen)
            after = camera_pose(screen)
            assert after[:3] != pytest.approx(before[:3], abs=1e-3), "no orbit"
            assert after[3:] == pytest.approx(before[3:], abs=1e-9), (
                "orbiting moved the target"
            )
            assert math.dist(after[:3], after[3:]) == pytest.approx(
                math.dist(before[:3], before[3:]), rel=1e-6
            )
            assert not looks_from(face), "letting go of the drag turned the camera"

            # From the front top edge to the back top edge, a turn straight
            # over the top would flip the camera as it passed overhead.
            turn_to([0, -1, 1], "the front top edge")
            motion = "Emulation.setEmulatedMedia"
            screen.selenium.execute_cdp_cmd(
                motion,
                {
                    "features": [
                        {"name": "prefers-reduced-motion", "value": "no-preference"}
                    ]
                },
            )
            try:
                elevations = probe(screen, "turnElevations", [0, 1, 1])
            finally:
                screen.selenium.execute_cdp_cmd(motion, {"features": []})
            assert elevations is not None, "the turn was not animated"
            assert max(elevations) < 50, elevations
            assert looks_from([0, 1 / math.sqrt(2), 1 / math.sqrt(2)])

    def test_the_scene_draws_only_when_something_in_it_moves(
        self, class_screen: Screen
    ) -> None:
        """Where WebGL is software-rendered a frame takes ~170 ms, so a
        scene redrawn while nothing changes pins the page. At rest it draws
        nothing; the arm moving draws."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        assert core.loop is not None

        async def ready():
            client = waldoctl.commander.client
            assert await client.simulator(True) == 1
            assert await client.reset() == 1
            assert await client.wait_command(await client.home(), timeout=15)

        asyncio.run_coroutine_threadsafe(ready(), core.loop).result(30)

        at_rest = frames_at_rest(screen, 1.0)
        # The footer's Waldo moving its eyes is no change to the scene.
        run_in_app(lambda: ui_state.readout_panel.look((1.0, 0.0, 0.0)))
        try:
            looked = frames_at_rest(screen, 1.0)
        finally:
            run_in_app(lambda: ui_state.readout_panel.look(None))
        assert looked == at_rest, (
            f"the scene drew {looked - at_rest} frames for a glance"
        )
        at_rest = frames_at_rest(screen, 1.0)

        async def nudge():
            client = waldoctl.commander.client
            q = list(await client.angles())
            q[0] += 3.0
            assert await client.wait_command(
                await client.move_j(q, duration=0.6), timeout=10
            )

        asyncio.run_coroutine_threadsafe(nudge(), core.loop).result(15)
        moved = frames_at_rest(screen, 0.5)
        assert moved > at_rest + 2, (at_rest, moved)

    def test_zoomed_out_the_fog_starts_beyond_the_robot(
        self, class_screen: Screen
    ) -> None:
        """A fog fixed to the reach swallowed the arm, its paths and targets
        once the camera pulled back past a couple of metres."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        reach = run_in_app(lambda: ui_state.urdf_scene._chain_reach())
        read = (
            "if (!S.scene.fog) return null;"
            "S.camera.position.set(0, -6, 6); S.controls.update();"
            "return {near: S.scene.fog.near, d: S.camera.position.length()};"
        )
        with camera_kept(screen):
            try:
                fog = wait(screen, 5).until(
                    lambda _: (m := scene_js(screen, read))
                    and m["near"] > m["d"] + reach
                    and m
                )
            except TimeoutException:
                fog = scene_js(screen, read)
            assert fog and fog["near"] > fog["d"] + reach, (fog, reach)

    def test_right_click_opens_a_context_menu_that_closes_on_an_outside_click(
        self, class_screen: Screen
    ) -> None:
        """A right-click opens the scene's menu, filled in by the app, whether
        the browser fires its contextmenu on the press or after the release;
        so does a touch held still. Each asks the app once, and a click on the
        scene closes the menu."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")

        def press_order() -> None:
            for kind in ("mousePressed", "mouseReleased"):
                screen.selenium.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": kind,
                        "x": round(x),
                        "y": round(y),
                        "button": "right",
                        "clickCount": 1,
                    },
                )

        def release_order() -> None:
            scene_js(
                screen,
                "const o = {clientX: arguments[0], clientY: arguments[1], bubbles: true,"
                " cancelable: true, button: 2, pointerId: 9, pointerType: 'mouse'};"
                "S.canvas.dispatchEvent(new PointerEvent('pointerdown', o));"
                "S.canvas.dispatchEvent(new PointerEvent('pointerup', o));"
                "S.canvas.dispatchEvent(new MouseEvent('contextmenu', o));",
                x,
                y,
            )

        def long_press() -> None:
            _touch(screen, "touchStart", [(3, x, y)])
            time.sleep(0.7)
            _touch(screen, "touchEnd", [])

        for ask in (press_order, release_order, long_press):

            def opened(ask=ask) -> None:
                ask()
                poll(lambda: js(screen, _MENU_ITEMS), 5.0, f"the menu ({ask.__name__})")
                _caught_up()

            sent = emits_during(screen, opened)
            assert sent.count("context") == 1, (ask.__name__, sent)
            items = js(screen, _MENU_ITEMS)
            assert "Target at robot position…" in items, (ask.__name__, items)

            ActionChains(screen.selenium).move_to_element(
                scene_canvas(screen)
            ).click().perform()
            wait(screen, 5).until(
                lambda _: no_visible(screen, ".q-menu"),
                message="Context menu should close when clicking outside",
            )

    def test_a_target_glows_under_the_pointer_and_right_clicks_to_its_menu(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        teleport_jog_pose()
        with camera_kept(screen), program_previewed(_TARGET_PROGRAM) as targets:
            assert targets, "the program shows no targets"
            group = f"targetgroup:{targets[-1]}"
            poll(lambda: scene_object(screen, group), 15.0, "the target")
            x, y = scene_object_pixel(screen, group)
            before = frames_at_rest(screen, 0.5)
            actions = ActionChains(screen.selenium, duration=0)
            pointer_to(screen, x, y, actions)
            actions.perform()
            poll(lambda: probe(screen, "glowing", group), 5.0, "the target to glow")
            assert frames_at_rest(screen, 0.5) > before, "the glow was never drawn"

            ActionChains(screen.selenium).context_click().perform()
            items = poll(lambda: js(screen, _MENU_ITEMS), 5.0, "the target's menu")
            assert "Edit Target..." in items and "Delete Target" in items, items
            canvas = scene_canvas(screen)
            ActionChains(screen.selenium).move_to_element(canvas).click().perform()
            wait(screen, 5).until(lambda _: no_visible(screen, ".q-menu"))

            before = frames_at_rest(screen, 0.5)
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
            )
            poll(lambda: not probe(screen, "glowing", group), 5.0, "the glow to go")
            assert frames_at_rest(screen, 0.5) > before, "the glow stayed drawn"

    def test_the_gizmo_arrow_jogs_on_a_drag_and_a_tap_keeps_it_pinned(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        teleport_jog_pose()
        urdf = ui_state.urdf_scene
        assert urdf is not None

        def shown() -> list[str]:
            return shown_handles(screen)

        with camera_kept(screen):
            # A mouse drag along the arrow, from where it sticks out past the
            # tool, jogs the tool.
            _hover_tcp(screen)
            poll(lambda: gizmo(screen, "tcp:ball"), 5.0, "the gizmo")
            x, y, dx, dy = poll(
                lambda: probe(screen, "arrowTip", "tcp:ball"), 5.0, "the X arrow"
            )
            # The gizmo stays while the pointer rests on its arrow, off the arm.
            actions = ActionChains(screen.selenium, duration=0)
            pointer_to(screen, x, y, actions)
            actions.perform()
            _caught_up()
            time.sleep(_HOVER_GRACE_S + 0.3)
            _caught_up()
            assert shown() == ["gizmo"], "the gizmo went from under the pointer"
            start = tcp_mm()
            camera = camera_pose(screen)

            # Sixty pointer moves along the arrow, a frame apart as a mouse
            # sends them: the value on the handle names the axis while it is
            # held, the app hears at most thirty targets a second, and the
            # marks go with the release.
            scene_js(
                screen,
                "const seen = window.__gizmoLabels = new Set();"
                "window.__sampling = true;"
                "(function sample() {"
                "  const f = document.querySelector('.wc-gizmo-field');"
                "  const v = f && !f.hidden && f.querySelector('.wc-gizmo-field-value');"
                "  if (v && !v.hidden) seen.add(v.getAttribute('aria-label'));"
                "  if (window.__sampling) requestAnimationFrame(sample);"
                "})();",
            )

            def drag() -> None:
                actions = ActionChains(screen.selenium, duration=16)
                pointer_to(screen, x, y, actions)
                actions.click_and_hold()
                for step in range(1, 61):
                    pointer_to(
                        screen,
                        x + dx * 40 * step / 60,
                        y + dy * 40 * step / 60,
                        actions,
                    )
                actions.release()
                actions.perform()
                _caught_up()

            timed = timed_emits_during(screen, drag)
            sent = [what for what, _ in timed]
            labels = js(
                screen, "window.__sampling = false; return [...window.__gizmoLabels];"
            )
            assert any(text and text.startswith("X ") for text in labels), labels
            gaps = np.diff([t for what, t in timed if what == "move"])
            assert (gaps >= 30.0).all(), f"targets {gaps.min():.1f} ms apart"
            assert sent[-1] == "end", sent
            assert scene_js(screen, "return S.label('tcp:label')") is None
            assert scene_object(screen, "tcp:ticks") is None, "the marks stayed"
            poll(
                lambda: np.linalg.norm(tcp_mm() - start) > 2.0,
                15.0,
                "the tool to move",
            )
            assert shown() == ["gizmo"], "the drag hid the gizmo"
            assert camera_pose(screen) == pytest.approx(camera, abs=1e-6), (
                "the drag orbited the camera"
            )
            # Once the arm comes to rest, the gizmo's frame is drawn where the
            # tool is.
            poll(
                lambda: np.linalg.norm(frame_mm(screen) - tcp_mm()) < 0.5,
                15.0,
                "the gizmo's frame on the tool",
            )

            # Touch uses the TCP badge; an arrow tap must retain that selection.
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
            )
            poll(lambda: shown() == [], 5.0, "the gizmo to hide")
            badge = screen.selenium.find_element(
                By.CSS_SELECTOR, '.wc-tcp-affordance button[aria-label="Move TCP"]'
            ).rect
            tap(
                screen,
                badge["x"] + badge["width"] / 2,
                badge["y"] + badge["height"] / 2,
            )
            poll(lambda: gizmo(screen, "tcp:ball"), 5.0, "the pinned gizmo")
            x, y, _, _ = poll(
                lambda: probe(screen, "arrowTip", "tcp:ball"), 5.0, "the X arrow"
            )
            tap(screen, x, y)
            _caught_up()
            assert shown() == ["gizmo"], "a tap on the gizmo's arrow unpinned it"

            tap(screen, *poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space"))
            poll(lambda: shown() == [], 5.0, "a tap on empty space to unpin")

    def test_a_right_drag_pans_without_opening_the_menu(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)

        def right_drag(x: float, y: float, offsets: list[int]) -> None:
            actions = ActionBuilder(screen.selenium)
            actions.pointer_action.move_to_location(round(x), round(y))
            actions.pointer_action.pointer_down(button=MouseButton.RIGHT)
            for dx in offsets:
                actions.pointer_action.move_to_location(round(x) + dx, round(y))
            actions.pointer_action.pointer_up(button=MouseButton.RIGHT)
            actions.perform()

        with camera_kept(screen):
            x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")
            # A menu left open by a right-click must not come back with a pan.
            # A press may show the menu for a moment; its release closes it.
            actions = ActionChains(screen.selenium, duration=0)
            pointer_to(screen, x, y, actions)
            actions.context_click()
            actions.perform()
            poll(lambda: js(screen, _MENU_ITEMS), 5.0, "the menu")
            camera = camera_pose(screen)

            def pan() -> None:
                right_drag(x, y, [10, 20, 30, 40, 50, 60])
                poll(lambda: camera_pose(screen) != camera, 5.0, "the camera to pan")
                _caught_up()

            sent = emits_during(screen, pan)
            assert "context" not in sent, f"a pan asked the app for a menu: {sent}"
            assert not js(screen, _MENU_ITEMS), "a right-drag brought back the menu"
            wait(screen, 5).until(
                lambda _: no_visible(screen, ".q-menu"),
                message="a right-drag left a menu open",
            )

            # A pan that comes back to where it started is still a pan.
            x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")

            def pan_back() -> None:
                right_drag(x, y, [20, 40, 60, 40, 20, 0])
                _caught_up()

            sent = emits_during(screen, pan_back)
            assert "context" not in sent, f"a pan back asked for a menu: {sent}"
            assert not js(screen, _MENU_ITEMS), (
                "a pan back to its start opened the menu"
            )

            # The menu key or a long press opens the menu with no right press
            # before it; headless Chrome makes no contextmenu from a synthetic
            # long press, so the event comes straight to the canvas.
            x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")
            scene_js(
                screen,
                "S.canvas.dispatchEvent(new MouseEvent('contextmenu', {clientX:"
                " arguments[0], clientY: arguments[1], bubbles: true, cancelable: true}));",
                x,
                y,
            )
            poll(lambda: js(screen, _MENU_ITEMS), 5.0, "the menu without a right press")
            ActionChains(screen.selenium).move_to_element(
                scene_canvas(screen)
            ).click().perform()
            wait(screen, 5).until(lambda _: no_visible(screen, ".q-menu"))

    def test_moving_between_links_shows_one_handle_at_a_time(
        self, class_screen: Screen
    ) -> None:
        """Hovering the arm shows one handle at a time, handing over from link
        to link and going once the pointer leaves the arm, all without a word
        to the app. Hidden keeps the gizmo away until it is allowed again; a
        target edit suspends the handles, and leaving it leaves no gizmo."""
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        teleport_jog_pose()

        def off_the_arm() -> None:
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
            )

        with camera_kept(screen):

            def tour() -> None:
                for link, shown in (
                    ("L2", "ring:1"),
                    ("L3", "ring:2"),
                    ("L6", "ring:5"),
                ):
                    hover_scene_object(screen, f"link:{link}")
                    poll(lambda: shown_handles(screen) == [shown], 5.0, shown)
                off_the_arm()
                poll(lambda: shown_handles(screen) == [], 5.0, "the handles to go")

            sent = emits_during(screen, tour)
            assert sent == [], f"hovering sent the app {len(sent)} events: {sent[:5]}"

            # Hidden: the gizmo stays away until it is allowed again. The TCP
            # badge is what reveals it, so its spot is where Hidden is tried,
            # and the same spot brings the gizmo back once allowed.
            wait_for_scene_idle(screen)
            badge = wait(screen).until(lambda _: _tcp_badge(screen)).rect
            _movement_handles(screen, False)
            try:
                action = ActionChains(screen.selenium)
                pointer_to(
                    screen,
                    badge["x"] + badge["width"] / 2,
                    badge["y"] + badge["height"] / 2,
                    action,
                )
                action.perform()
                _caught_up()
                time.sleep(_HOVER_GRACE_S + 0.1)
                assert _tcp_badge(screen) is None, "Hidden left the TCP badge shown"
                assert "gizmo" not in shown_handles(screen), "Hidden showed the gizmo"
            finally:
                _movement_handles(screen, True)
            _hover_tcp(screen)
            poll(lambda: shown_handles(screen) == ["gizmo"], 5.0, "the gizmo back")
            off_the_arm()
            poll(lambda: shown_handles(screen) == [], 5.0, "the gizmo to go")

            # Editing a target moves the target: the arm's links reveal no
            # dials while it is open, and leaving clears its gizmo.
            x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")
            choose(screen, x, y, "Target at robot position…")
            try:
                poll(lambda: gizmo(screen, "tcp:ball"), 10.0, "the editing gizmo")
                hover_scene_object(screen, "link:L2")
                _caught_up()
                time.sleep(_HOVER_GRACE_S + 0.1)
                assert "ring:1" not in shown_handles(screen), (
                    "the edit revealed a joint dial"
                )
            finally:
                end_target_edit(screen)
            off_the_arm()
            poll(lambda: shown_handles(screen) == [], 5.0, "no gizmo after the edit")

    def test_the_rotate_gizmo_turns_the_tool_and_leaves_the_camera(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        teleport_jog_pose()

        def rpy() -> np.ndarray:
            pose = waldoctl.commander.status.pose
            return np.array([pose.rx, pose.ry, pose.rz], dtype=float)

        try:
            with camera_kept(screen):
                _hover_tcp(screen)
                deadline = time.monotonic() + 5.0
                while not (
                    spot := next(
                        (s for a in "ZXY" if (s := probe(screen, "ringSpot", a))), None
                    )
                ):
                    assert time.monotonic() < deadline, "no ring of the gizmo is free"
                    time.sleep(0.1)
                x, y, dx, dy = spot
                start = rpy()
                camera = camera_pose(screen)
                _drag_along(screen, x, y, dx, dy)
                poll(
                    lambda: np.abs(rpy() - start).max() > 2.0,
                    15.0,
                    "the tool to turn",
                )
                assert camera_pose(screen) == pytest.approx(camera, abs=1e-6), (
                    "the drag orbited the camera"
                )
        finally:
            teleport_jog_pose()

    def test_orbit_wheel_and_reset_move_the_camera(self, class_screen: Screen) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        urdf = ui_state.urdf_scene
        assert urdf is not None
        default = [
            DEFAULT_CAMERA["x"],
            DEFAULT_CAMERA["y"],
            DEFAULT_CAMERA["z"],
            0.0,
            0.0,
            DEFAULT_CAMERA["look_at_z"],
        ]
        with camera_kept(screen):
            click_marked(screen, "camera-reset")
            poll(
                lambda: camera_pose(screen) == pytest.approx(default, abs=1e-6),
                5.0,
                "the default view",
            )

            x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")
            _drag_along(screen, x, y, 1, 0, 80)
            orbited = camera_pose(screen)
            assert orbited[:3] != pytest.approx(default[:3], abs=1e-3), "no orbit"
            assert orbited[3:] == pytest.approx(default[3:], abs=1e-6), (
                "orbiting moved the target"
            )

            canvas = scene_canvas(screen)
            ActionChains(screen.selenium).scroll_from_origin(
                ScrollOrigin.from_element(canvas), 0, 500
            ).perform()
            poll(
                lambda: math.dist(camera_pose(screen)[:3], camera_pose(screen)[3:])
                > math.dist(orbited[:3], orbited[3:]),
                5,
                "the camera to zoom out",
            )

            click_marked(screen, "camera-reset")
            poll(
                lambda: camera_pose(screen) == pytest.approx(default, abs=1e-6),
                5.0,
                "the default view again",
            )
            # Once there, the camera is free to orbit at once.
            _drag_along(screen, x, y, 1, 0, 80)
            assert camera_pose(screen)[:3] != pytest.approx(default[:3], abs=1e-3), (
                "the reset held the camera at the default view"
            )

    def test_a_target_is_placed_edited_and_deleted_from_the_scene(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        teleport_jog_pose()
        urdf = ui_state.urdf_scene
        assert urdf is not None

        def lines() -> list[str]:
            return run_in_app(lambda: ui_state.active_textarea.value).splitlines()

        def requested() -> tuple[float, ...] | None:
            return run_in_app(lambda: urdf._requested_edit_pose)

        def drag_the_gizmo() -> None:
            """Drag the target's arrow: the target moves and is kept, the arm
            does not move; then put the edit away."""
            poll(lambda: gizmo(screen, "tcp:ball"), 10.0, "the editing gizmo")
            start = requested()
            source = run_in_app(lambda: ui_state.active_textarea.value)
            live = waldoctl.commander.status.joints.angles.rad.copy()
            x, y, dx, dy = poll(
                lambda: probe(screen, "arrowTip", "tcp:ball"), 5.0, "the X arrow"
            )
            _drag_along(screen, x, y, dx, dy)
            poll(lambda: requested() != start, 10.0, "the target to move")
            poll(
                lambda: run_in_app(lambda: ui_state.active_textarea.value) != source,
                10.0,
                "the release to be written",
            )
            assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)
            end_target_edit(screen)

        def target_on(line_number: int) -> str:
            """The marker of the target on *line_number*, once the preview
            answers the source: until then the target's menu is disabled."""

            def find() -> str | None:
                tab = waldoctl.commander.programs.active
                assert tab is not None
                if not _preview_settled():
                    return None
                for target in tab.dry_run.targets:
                    if target.line_number == line_number and not target.id.startswith(
                        "pending_"
                    ):
                        return f"targetgroup:{target.id}"
                return None

            group = poll(
                lambda: run_in_app(find), 15.0, f"a target on line {line_number}"
            )
            poll(lambda: scene_object(screen, group), 15.0, group)
            return group

        with camera_kept(screen), program_previewed(_TARGET_PROGRAM):
            try:
                before = lines()

                # Place a target at the arm, drag it away, and keep it.
                x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")
                choose(screen, x, y, "Target at robot position…")
                drag_the_gizmo()
                after = poll(
                    lambda: len(now := lines()) == len(before) + 1 and now,
                    10.0,
                    "a new line",
                )
                (added,) = [i for i, line in enumerate(after) if line not in before]
                assert "move_l(" in after[added], after[added]

                # Edit it: dragging its gizmo rewrites its line.
                group = target_on(added + 1)
                choose(screen, *scene_object_pixel(screen, group), "Edit Target...")
                drag_the_gizmo()
                poll(lambda: lines()[added] != after[added], 10.0, "the line to change")
                assert "move_l(" in lines()[added], lines()[added]

                # Escape in the code editor is the editor's own; anywhere else
                # it puts a new placement away, keeping it, and neither stops
                # the arm. The target placed above may now cover the first
                # empty spot.
                edited = lines()
                x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")
                choose(screen, x, y, "Target at robot position…")
                poll(lambda: gizmo(screen, "tcp:ball"), 10.0, "the editing gizmo")

                # While the target is open the arm is drawn where it is.
                live = list(waldoctl.commander.status.joints.angles.rad)
                poll(
                    lambda: _drawn_at_fk(screen, urdf, "L2", live),
                    5.0,
                    "L2 at the live pose",
                )
                stops = motion_guard.stop_generation
                click_tab(screen, "program")
                try:
                    editor = focus_editor(screen)
                    poll(
                        lambda: keybindings_manager.editor_focused,
                        5.0,
                        "the editor to have the keyboard",
                    )
                    editor.send_keys(Keys.ESCAPE)
                    _caught_up()
                    assert run_in_app(lambda: urdf._editing_unified_target), (
                        "Escape in the editor cancelled the placement"
                    )
                finally:
                    close_panel(screen, "program-panel")
                defocus_editor(screen)
                send_global_key(screen, Keys.ESCAPE)
                poll(
                    lambda: not run_in_app(lambda: urdf._editing_unified_target),
                    5.0,
                    "Escape to put the edit away",
                )
                _caught_up()
                assert motion_guard.stop_generation == stops, "Escape stopped the arm"
                assert len(lines()) == len(after) + 1, "the placed target was lost"
                # Ctrl+Z from the scene takes the placement back out.
                ActionChains(screen.selenium).key_down(Keys.CONTROL).send_keys(
                    "z"
                ).key_up(Keys.CONTROL).perform()
                poll(lambda: lines() == edited, 10.0, "Ctrl+Z to undo the placement")

                # Delete it, straight from the menu.
                group = target_on(added + 1)
                choose(screen, *scene_object_pixel(screen, group), "Delete Target")
                poll(lambda: lines() == before, 10.0, "the line to go")
                # Its marker goes with it, not a second later with the next
                # preview, so it cannot be deleted twice.
                poll(
                    lambda: scene_object(screen, group) is None,
                    0.3,
                    "the marker to go",
                )
            finally:
                if run_in_app(lambda: urdf._editing_unified_target):
                    end_target_edit(screen)

    def test_a_keep_out_is_placed_moved_and_deleted_from_the_scene(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        handle = waldoctl.commander.scene

        def shapes() -> dict[str, tuple[float, ...]]:
            return run_in_app(lambda: {s.name: tuple(s.pose) for s in handle.shapes})

        def gizmo_on(name: str) -> bool:
            return gizmo(screen, name) is not None

        before = shapes()
        with camera_kept(screen):
            try:
                x, y, *floor = poll(
                    lambda: probe(screen, "floorSpot"), 5.0, "floor near the arm"
                )
                offered = choose(screen, x, y, "Box here")
                assert any(i.startswith("Place Target Here") for i in offered), offered
                (name,) = poll(
                    lambda: set(shapes()) - set(before), 5.0, "the new keep-out"
                )
                assert shapes()[name][:2] == pytest.approx(floor, abs=0.003), (
                    "the keep-out is not where the floor was clicked"
                )
                obj = f"shape:{name}"
                # Move it by its gizmo's arrow; the drop is its new pose.
                clear_selection(screen)
                centre = settled_pixel(screen, obj)
                choose(screen, *centre, "Move (drag arrows)")
                poll(lambda: gizmo_on(obj), 5.0, "the keep-out's gizmo")
                start = shapes()[name]
                x, y, dx, dy = poll(
                    lambda: probe(screen, "arrowTip", obj), 5.0, "the X arrow"
                )
                _drag_along(screen, x, y, dx, dy)
                poll(
                    lambda: shapes()[name][0] != start[0], 10.0, "the keep-out to move"
                )
                assert shapes()[name][1:] == pytest.approx(start[1:], abs=1e-6)
                centre = settled_pixel(screen, obj)
                choose(screen, *centre, "Stop Moving")
                poll(lambda: not gizmo_on(obj), 5.0, "the gizmo to go")

                choose(screen, *centre, "Delete")
                poll(lambda: name not in shapes(), 5.0, "the keep-out to go")
            finally:
                run_in_app(
                    lambda: setattr(
                        handle, "shapes", [s for s in handle.shapes if s.name in before]
                    )
                )

    def test_held_object_follows_the_flange(self, class_screen: Screen) -> None:
        """A shape attached to the flange is parented to the last actuated
        link, so its world matrix in three.js is the Python FK of the pose
        times its flange offset, at every pose."""
        import parol6.PAROL6_ROBOT as model

        screen = class_screen
        screen_wait_for_scene_ready(screen)
        assert core.loop is not None
        local = (0.03, 0.02, 0.25, 0.2, -0.3, 0.4)

        async def prepare():
            client = waldoctl.commander.client
            assert await client.simulator(True) == 1
            assert await client.reset() == 1
            index = await client.home()
            assert await client.wait_command(index, timeout=15)
            index = await client.select_tool("NONE")
            assert await client.wait_command(index, timeout=5)
            world = await client.shapes()
            assert world is not None
            part = Box(name="held-part", x=0.045, y=0.07, z=0.1).attach(
                flange_pose=local,
                epoch=world.attachment_epoch,
            )
            assert await client.set_shapes([part]) == 1
            await waldoctl.commander.scene.refresh_from_backend()
            return np.radians(await client.angles())

        joints = asyncio.run_coroutine_threadsafe(prepare(), core.loop).result(30)
        scene = ui_state.urdf_scene
        assert scene is not None

        def at_pose(q):
            scene.set_appearance_mode(RobotAppearanceMode.EDITING)
            scene.set_editing_angles(q.tolist())

        try:
            for turn in (0, 0.3):
                q = joints.copy()
                q[0] += turn
                expected = model.robot.fkine(q) @ model._pose_to_matrix(local)
                run_in_app(lambda: at_pose(q))

                def matches(_driver):
                    actual = scene_js(screen, _HELD_MATRIX)
                    return actual is not None and np.allclose(
                        np.asarray(actual).reshape((4, 4), order="F"),
                        expected,
                        atol=1e-6,
                    )

                wait(screen, 20).until(matches)
        finally:
            asyncio.run_coroutine_threadsafe(
                waldoctl.commander.client.set_shapes([]), core.loop
            ).result(10)
            run_in_app(lambda: scene.set_appearance_mode(RobotAppearanceMode.LIVE))

    def test_a_simulated_run_paints_its_contacts_and_com(
        self, class_screen: Screen
    ) -> None:
        """The contact arrows and the centre-of-mass marker are three.js
        geometry: whether they exist in Python says nothing about
        whether anything reaches the canvas. The installed backend plans and
        does not simulate, so the commanded/predicted pair is injected, shaped
        as a backend produces it; everything downstream is the shipping code."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        saved_view: dict[str, bool] = {}

        def populate() -> waldoctl.TickIndex:
            from waldo_commander.services.preview_segments import segments_from_record
            from waldo_commander.state import simulation_state

            view = waldoctl.commander.settings.view
            saved_view.update({f: getattr(view, f) for f in _VIEW_FLAGS})
            for flag in _VIEW_FLAGS:
                setattr(view, flag, True)
            program = waldoctl.commander.programs.active
            assert program is not None
            # A predicted pass always answers a plan, so a predicted record on
            # screen always has the commanded one and its segments beside it.
            commanded, predicted = _records(waldoctl.commander.status.joints.angles.rad)
            program.dry_run.commanded = commanded
            program.dry_run.commanded_revision = 1
            program.dry_run.predicted = predicted
            program.dry_run.predicted_revision = 1
            program.dry_run.path_segments = segments_from_record(commanded, [])
            program.dry_run.total_steps = 1
            simulation_state.notify_changed()
            return predicted

        def seek(ticks: waldoctl.TickIndex) -> None:
            from waldo_commander.components.playback import playback

            # The frame annotations ride the playback batch, so put playback
            # inside the contact window through the real seek path.
            playback.invalidate_timeline()
            playback.update_scrub_segments()
            assert playback._ensure_timeline() is not None, "no timeline to seek in"
            playback._apply_time(ticks.duration_s * 0.5)

        def restore() -> None:
            """No record, no segments, no timeline, and the view settings as
            they were: a record left behind would be replayed by the next test
            that touches playback, and the view flags are process-wide."""
            from waldo_commander.components.playback import playback
            from waldo_commander.state import simulation_state

            view = waldoctl.commander.settings.view
            for flag, value in saved_view.items():
                setattr(view, flag, value)
            program = waldoctl.commander.programs.active
            if program is not None:
                program.dry_run.commanded = None
                program.dry_run.commanded_revision = -1
                program.dry_run.predicted = None
                program.dry_run.predicted_revision = -1
                program.dry_run.path_segments = []
                program.dry_run.total_steps = 0
                program.dry_run.playback.playback_time = 0.0
            playback.invalidate_timeline()
            simulation_state.notify_changed()

        def overlay() -> dict | None:
            return scene_js(screen, _OVERLAY_JS)

        with window_size(screen, 1280, 900):
            # The scrub bar and its playback controls live on the program tab,
            # and seeking is what drives the per-frame annotations.
            click_tab(screen, "program")
            ticks = run_in_app(populate)
            try:
                try:
                    wait(screen, 15).until(
                        lambda _: (i := overlay()) and i.get("meshes")
                    )
                except TimeoutException:
                    pass
                run_in_app(lambda: seek(ticks))
                try:
                    wait(screen).until(
                        lambda _: (i := overlay()) and i.get("shown", 0) >= 2
                    )
                except TimeoutException:
                    pass
                info = overlay()

                assert info is not None, "no three.js scene on the page"
                assert info.get("found"), (
                    "the physics overlay group never reached the scene"
                )
                assert info["meshes"] >= 2, (
                    f"the per-frame pool was never created: {info}"
                )
                assert info["shown"] >= 2, (
                    f"expected contact arrows and the centre-of-mass marker to be "
                    f"shown at a frame that has contacts: {info}"
                )

                # An unchanged pair keeps its pool, or the arrows would blink at
                # every change notice; a prediction that is the plan annotates
                # nothing.
                from waldo_commander.state import simulation_state

                overlay_service = ui_state.urdf_scene.physics_overlay
                built = run_in_app(lambda: overlay_service._group)
                run_in_app(simulation_state.notify_changed)
                _caught_up()
                assert run_in_app(lambda: overlay_service._group) is built, (
                    "an unchanged prediction rebuilt the pool"
                )

                def plan_as_prediction() -> None:
                    dry_run = waldoctl.commander.programs.active.dry_run
                    dry_run.predicted = dry_run.commanded
                    simulation_state.notify_changed()

                run_in_app(plan_as_prediction)
                wait(screen).until(
                    lambda _: not run_in_app(lambda: overlay_service.is_built)
                )
            finally:
                run_in_app(restore)

    def test_a_lost_context_is_drawn_again_and_still_answers(
        self, class_screen: Screen
    ) -> None:
        """The GPU taking the view's context away and giving it back: the view
        is drawn again as it was, without the app resending it, and the
        camera, the gizmo and the view cube still answer the pointer."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        if not scene_js(
            screen,
            "return !!S.renderer.getContext().getExtension('WEBGL_lose_context')",
        ):
            pytest.skip("this browser cannot lose a WebGL context on request")
        teleport_jog_pose()
        pixels = """
            const at = S.hoverPixel('link:L3');
            S.renderer.render(S.scene, S.camera);
            const gl = S.renderer.getContext();
            const r = S.canvas.getBoundingClientRect();
            const dpr = S.renderer.getPixelRatio();
            const read = (px, py) => { const out = new Uint8Array(4);
              gl.readPixels(Math.round((px - r.left) * dpr), Math.round((r.bottom - py) * dpr),
                            1, 1, gl.RGBA, gl.UNSIGNED_BYTE, out);
              return Array.from(out).slice(0, 3); };
            return {link: at && read(at[0], at[1])};
        """
        with camera_kept(screen):
            before = poll(lambda: scene_js(screen, pixels)["link"], 10.0, "L3 drawn")
            resets = scene_js(screen, "return S.resets")
            scene_js(screen, "S.renderer.forceContextLoss()")
            poll(
                lambda: js(
                    screen, "return !!document.querySelector('.wc-scene[data-gl]')"
                ),
                5.0,
                "the view to know its context is gone",
            )
            scene_js(screen, "S.renderer.forceContextRestore()")
            poll(
                lambda: js(
                    screen, "return !document.querySelector('.wc-scene[data-gl]')"
                ),
                5.0,
                "the context back",
            )
            after = poll(lambda: scene_js(screen, pixels)["link"], 10.0, "L3 again")
            assert after == pytest.approx(before, abs=6), (before, after)
            assert scene_js(screen, "return S.resets") == resets, (
                "restoring the context had the app send the scene again"
            )

            # The gizmo still comes on a hover.
            _hover_tcp(screen)
            poll(lambda: gizmo(screen, "tcp:ball"), 5.0, "the gizmo")

            # The camera still orbits under a drag on empty space.
            camera = camera_pose(screen)
            x, y = poll(lambda: probe(screen, "emptySpot"), 5.0, "empty space")
            _drag_along(screen, x, y, 1, 0, 80)
            assert camera_pose(screen) != pytest.approx(camera, abs=1e-6), "no orbit"

            # The view cube still turns the camera.
            cx, cy, face = poll(
                lambda: probe(screen, "cubeSpot"), 5.0, "a face of the view cube"
            )
            actions = ActionChains(screen.selenium, duration=0)
            pointer_to(screen, cx, cy, actions)
            actions.click()
            actions.perform()
            poll(
                lambda: np.dot(probe(screen, "viewDirection"), face) > 0.999,
                5.0,
                f"the camera to look from {face}",
            )
