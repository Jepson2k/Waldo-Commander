"""Shared steps for the scene's browser tests: previews, menus, handles,
the camera, and the editing inspectors, each through the real page."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import numpy as np
import waldoctl
from nicegui import core
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import click_marked, marked_element, run_in_app
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.scene_surface import (
    hover_scene_object,
    pointer_to,
    project_local,
    scene_js,
    scene_object_pixel,
    shown_handles,
    wait_for_scene_idle,
)
from tests.helpers.wait import JOG_SAFE_POSE_DEG
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


def probe(screen: Screen, name: str, *args):
    """A probe of the scene's surface: a pixel where a press grabs what is
    asked for, or None while there is none."""
    return scene_js(screen, f"return S.{name}(...arguments)", *args)


def camera_pose(screen: Screen) -> list[float]:
    return scene_js(screen, "return S.cameraPose()")


def zoom(screen: Screen, distance: float) -> None:
    scene_js(screen, "S.zoom(arguments[0])", distance)


def marker_mm(screen: Screen, name: str) -> np.ndarray:
    """Where the named scene object is in the world, in mm."""
    position = scene_js(
        screen,
        "const o=S.byName(arguments[0]);o.updateWorldMatrix(true,false);"
        "return o.getWorldPosition(o.position.clone()).toArray();",
        name,
    )
    return np.array(position) * 1000


def gizmo(screen: Screen, name: str, mark: bool = False) -> dict | None:
    """The snaps of the gizmo on ``name``; ``mark`` tags it so a re-attach shows."""
    return scene_js(screen, "return S.gizmo(arguments[0], arguments[1])", name, mark)


def poll(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def joint_deg(i: int) -> float:
    return float(waldoctl.commander.status.joints.angles.deg[i])


def joint_settled(i: int, timeout: float = 20.0) -> float:
    """The joint's value once it has stopped moving."""
    deadline = time.monotonic() + timeout
    last = joint_deg(i)
    while time.monotonic() < deadline:
        time.sleep(0.3)
        now = joint_deg(i)
        if abs(now - last) < 0.005:
            return now
        last = now
    raise AssertionError(f"J{i + 1} never settled")


def teleport_jog_pose() -> None:
    assert core.loop is not None
    # A released drag still driving to its target would pull the arm back.
    run_in_app(ui_state.control_panel.cancel_drags)
    client = ui_state.control_panel.client
    asyncio.run_coroutine_threadsafe(
        client.teleport(JOG_SAFE_POSE_DEG), core.loop
    ).result(10)
    poll(
        lambda: np.allclose(
            waldoctl.commander.status.joints.angles.deg, JOG_SAFE_POSE_DEG, atol=0.05
        ),
        10.0,
        "teleport joints readback",
    )


@contextmanager
def camera_kept(screen: Screen) -> Iterator[None]:
    """Give the next test the camera this one found, and take the pointer off
    the arm so no handle stays revealed."""
    saved = camera_pose(screen)
    try:
        yield
    finally:
        scene_js(screen, "S.setCameraPose(arguments[0])", saved)
        screen.selenium.execute_cdp_cmd(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
        )
        # Out of their grace, so the next test starts with none shown.
        deadline = time.monotonic() + 2.0
        while shown_handles(screen) and time.monotonic() < deadline:
            time.sleep(0.05)


EDIT_PROGRAM = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5)
# Fixture approach · keep this annotation
rbt.move_l(
    pose=[0, 280, 250,  # TCP position · mm
          90, 0, 90],  # Tool orientation · °
    speed=0.5,
)
"""


PATH_PROGRAM = """from parol6 import RobotClient
import time
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5)
rbt.move_l([0, 260, 300, 90, 0, 90], speed=0.5)
rbt.move_l([-110, 210, 250, 90, 0, 90], speed=0.5)
rbt.move_l([90, 330, 250, 90, 0, 90], speed=0.5)
time.sleep(0.8)
rbt.move_l([-90, 330, 250, 90, 0, 90], speed=0.5)
rbt.move_l([110, 210, 250, 90, 0, 90], speed=0.5)
rbt.move_l([110, 210, 250, 90, 0, 105], speed=0.5)
"""


#: A camera on the middle of PATH_PROGRAM's path, close enough to follow it.
PATH_VIEW = [0.169, 0.418, 0.401, 0, 0.27, 0.25]

#: A camera close on the tool at the jog pose, where its gizmo sits.
TCP_VIEW = [0.229, 0.409, 0.369, 0.015, 0.222, 0.179]


@contextmanager
def program_previewed(source: str) -> Iterator[list[str]]:
    """Preview ``source`` in the scene and yield its target ids; the program
    that was there comes back afterwards, each through the editor as an edit
    would come."""
    from waldo_commander.components.simulation_engine import simulation

    def edit(text: str) -> None:
        ui_state.active_textarea.value = text  # schedules the preview

    def previewing() -> bool:
        return (
            simulation._simulation_debounce_timer is not None
            or simulation._physics_timer is not None
        )

    def saved() -> tuple[str, object]:
        tab = waldoctl.commander.programs.active
        assert tab is not None
        return ui_state.active_textarea.value, tab.dry_run.last_sim_joints_deg

    text, sim_joints = run_in_app(saved)
    run_in_app(lambda: edit(source))
    poll(lambda: not run_in_app(previewing), 30.0, "the program's preview")
    try:
        yield run_in_app(
            lambda: [t.id for t in waldoctl.commander.programs.active.dry_run.targets]
        )
    finally:
        run_in_app(lambda: edit(text))
        # A preview still running when the next test injects its records
        # would overwrite them.
        poll(lambda: not run_in_app(previewing), 30.0, "the restored preview")
        # A previewed program re-previews whenever the arm moves; the program
        # that was there had not been previewed.
        run_in_app(
            lambda: setattr(
                waldoctl.commander.programs.active.dry_run,
                "last_sim_joints_deg",
                sim_joints,
            )
        )


def tcp_mm() -> np.ndarray:
    pose = waldoctl.commander.status.pose
    return np.array([pose.x, pose.y, pose.z], dtype=float)


def ball_mm(screen: Screen) -> np.ndarray:
    """Where the browser draws the gizmo's ball, in mm: during a drag, where
    the drag has taken it; the frame stays where the drag began."""
    return (
        np.array(
            scene_js(
                screen,
                "const b = S.byName('tcp:ball');"
                "return b.getWorldPosition(b.position.clone()).toArray();",
            )
        )
        * 1000.0
    )


def frame_mm(screen: Screen) -> np.ndarray:
    """Where the browser draws the gizmo's frame, in mm."""
    return (
        np.array(
            scene_js(
                screen,
                "const f = S.byName('tcp:ball_frame');"
                "return f.getWorldPosition(f.position.clone()).toArray();",
            )
        )
        * 1000.0
    )


def at_start(screen):
    """No drag in progress, the arm at the jog pose and the scene caught up."""
    run_in_app(ui_state.control_panel.cancel_drags)
    teleport_jog_pose()
    wait(screen).until(
        lambda _: np.allclose(
            waldoctl.commander.status.joints.angles.deg, JOG_SAFE_POSE_DEG, atol=0.1
        )
    )
    wait(screen).until(lambda _: np.linalg.norm(frame_mm(screen) - tcp_mm()) < 0.5)
    wait_for_scene_idle(screen)


def hold(screen, spot, length=20, *, kind="tcp"):
    """Press the handle at ``spot`` and drag it ``length`` px along its axis,
    still held."""
    x, y, dx, dy = spot
    action = ActionChains(screen.selenium, duration=40)
    pointer_to(screen, x, y, action)
    action.click_and_hold()
    for k in range(1, 11):
        pointer_to(screen, x + dx * length * k / 10, y + dy * length * k / 10, action)
    action.perform()
    wait(screen).until(
        lambda _: scene_js(screen, "return !!S.byName(arguments[0])", f"{kind}:ticks")
    )


def click_spot(screen, spot) -> None:
    """Press and release the handle at ``spot`` without moving: a click."""
    action = ActionChains(screen.selenium, duration=0)
    pointer_to(screen, spot[0], spot[1], action)
    action.click()
    action.perform()


def gizmo_field(screen) -> dict | None:
    """The value riding on a gizmo handle, or None when none shows: whether
    it is being typed, its shown value and unit, what it names (its
    aria-label), and the typed text."""
    return scene_js(
        screen,
        """
        const f = [...document.querySelectorAll('.wc-gizmo-field')].find((e) => !e.hidden);
        if (!f) return null;
        const v = f.querySelector('.wc-gizmo-field-value'), i = f.querySelector('input');
        return {typing: !i.hidden, value: v.textContent,
                unit: f.querySelector('.wc-gizmo-field-unit').textContent,
                label: (i.hidden ? v : i).getAttribute('aria-label') || '',
                input: i.value};
        """,
    )


def field_number(text: str) -> float:
    """A value as the field shows it ("+12", "−3.5") as a number."""
    return float(text.replace("−", "-"))


def grab_ring(screen: Screen, link: str, u: int) -> tuple[dict, float]:
    """Reveal *link*'s dial by hovering its visible surface and find a free grip."""
    deadline = time.monotonic() + 10.0
    while True:
        hover_scene_object(screen, link)
        try:
            dial = poll(lambda: probe(screen, "dial", u), 2.0, f"J{u + 1}'s ring")
            grab = probe(screen, "ringGrab", u)
        except AssertionError:
            dial = grab = None
        if grab is not None:
            return dial, grab
        if time.monotonic() > deadline:
            cover = probe(screen, "ringGrab", u, True)
            shown = shown_handles(screen)
            state = scene_js(
                screen,
                "const c = getElement(S.root).core; const h = c.pointer.hover;"
                "return {target: h.target, shown: h.shown, pinned: h.pinned,"
                " rules: c.ix.rules, edit: c.ix.edit && c.ix.edit.active,"
                " active: c.gestures.active, hovered: c.pointer.hovered};",
            )
            raise AssertionError(
                f"no part of J{u + 1}'s ring is uncovered: {cover}, showing {shown},"
                f" hover {state}"
            )
        time.sleep(0.2)


def drag_dial(screen, degrees, joint=0, duration=60, at_knob=False):
    """Drag joint ``joint``'s dial ``degrees`` from a free grip (or its knob),
    still held."""
    dial, grab = grab_ring(screen, f"link:L{joint + 1}", joint)
    if at_knob:
        grab = 0
    at = dial["q"] + math.radians(grab)
    path = project_local(
        screen,
        f"jog:dial:{joint}",
        [
            [
                dial["r"] * math.cos(at + math.radians(degrees) * k / 12),
                dial["r"] * math.sin(at + math.radians(degrees) * k / 12),
                0,
            ]
            for k in range(13)
        ],
    )
    actions = ActionChains(screen.selenium, duration=duration)
    pointer_to(screen, *path[0], actions)
    actions.click_and_hold()
    for point in path[1:]:
        pointer_to(screen, *point, actions)
    actions.perform()
    wait(screen).until(
        lambda _: scene_js(
            screen,
            f"return S.byName('jog:dial:{joint}:start')?.visible && S.byName('jog:dial:{joint}:sweep')?.visible",
        )
    )


def assert_dial_readable(screen, joint=0):
    """The dial's readout stays on screen, clear of its marks and its knob."""
    problems = scene_js(
        screen,
        """
        const name = `jog:dial:${arguments[0]}`;
        const badge = S.byName(`${name}:label`).element.getBoundingClientRect();
        const overlaps = (box) => badge.left < box.right && badge.right > box.left && badge.top < box.bottom && badge.bottom > box.top;
        const problems = [];
        if (badge.left < 0 || badge.top < 0 || badge.right > innerWidth || badge.bottom > innerHeight) problems.push('readout outside viewport');
        S.byName(name).traverse((object) => {
            if (!object.element || object.name === `${name}:label`) return;
            const visible = getComputedStyle(object.element).visibility !== 'hidden' && object.element.style.display !== 'none';
            if (object.userData.limit && !visible) problems.push(`${object.name} is hidden`);
            if (visible && overlaps(object.element.getBoundingClientRect())) problems.push(`${object.name} overlaps readout`);
        });
        const [x, y] = S.project(`${name}:knob`, [[S.dial(arguments[0]).r, 0, 0]])[0];
        if (overlaps({left: x - 8, right: x + 8, top: y - 8, bottom: y + 8})) problems.push('readout covers active handle');
        return problems;
        """,
        joint,
    )
    assert not problems, problems


KEEPOUT_SUBMENUS = ("Create keep-out", "Draw keep-out")


def choose(screen: Screen, x: float, y: float, item: str) -> list[str]:
    """Right-click at (x, y), pick ``item`` from the scene's menu and return
    the items the menu offered."""
    actions = ActionChains(screen.selenium, duration=0)
    pointer_to(screen, x, y, actions)
    actions.context_click()
    actions.perform()

    offered: list[str] = []
    clicked: set[str] = set()

    def shown_item(driver):
        shown = [
            e
            for e in driver.find_elements(By.CSS_SELECTOR, ".q-menu .q-item")
            if e.is_displayed()
        ]
        texts = [e.text.strip() for e in shown]
        if item not in texts and (item.endswith(" here") or item.startswith("Draw ")):
            # Empty space files keep-outs under "Create keep-out", an object's
            # face under "Draw keep-out".
            submenu = next(
                (
                    e
                    for e, text in zip(shown, texts)
                    if text.split("\n")[0] in KEEPOUT_SUBMENUS
                ),
                None,
            )
            # Clicked again before it opens, the submenu would close; a menu
            # filled in anew has a new entry to click.
            if submenu is not None and submenu.id not in clicked:
                submenu.click()
                clicked.add(submenu.id)
                return False
        # The menu fills in after it opens; pick from it once it stops changing.
        settled, offered[:] = texts == offered, texts
        if not settled or item not in texts:
            return False
        choice = shown[texts.index(item)]
        assert choice.get_attribute("aria-disabled") != "true", f"{item!r} is disabled"
        choice.click()
        return True

    # The menu is filled in after it opens, so its items can go stale while read.
    try:
        WebDriverWait(
            screen.selenium,
            5,
            poll_frequency=0.05,
            ignored_exceptions=(StaleElementReferenceException,),
        ).until(shown_item)
    except TimeoutException:
        browser = scene_js(
            screen,
            "const c=getElement(S.root).core;"
            "return {blocks:[...c.gestures.blocks],live:c.live,"
            "gesture:c.gestures.active,drag:!!c.pointer.drag,gen:c.menu.gen,"
            "pending:c.menu.pending&&c.menu.pending.gen,epoch:c.gestures.epoch}",
        )
        app = run_in_app(
            lambda: {
                "epoch": ui_state.urdf_scene.scene.gestures.epoch,
                "control": ui_state.urdf_scene._has_control(),
            }
        )
        raise AssertionError(
            f"no {item!r} in the menu: {offered}; browser {browser}, app {app}"
        ) from None
    # A closing menu still covers the canvas and would swallow the next press.
    # The app closes it, after whatever the item set off.
    wait(screen, 10).until(lambda _: no_visible(screen, ".q-menu"))
    return offered


def fill(screen, marker, value):
    """Type ``value`` into the input marked ``marker`` and press Enter. A
    card being rebuilt or seated can briefly hold an input that takes no
    keys, so the input is looked up again until one does."""
    opened = False

    def typed(_) -> bool:
        nonlocal opened
        try:
            element = marked_element(screen, marker)
            if element.tag_name != "input":
                element = element.find_element(By.CSS_SELECTOR, "input")
            if not element.is_displayed() and marker in (
                "object-rx",
                "object-ry",
                "object-rz",
            ):
                # A short card scrolls, and its sticky actions can cover the
                # header; clicked again while opening, it would close.
                if not opened:
                    screen.selenium.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'})",
                        marked_element(screen, "object-orientation"),
                    )
                    click_marked(screen, "object-orientation")
                    opened = True
                return False
            screen.selenium.execute_script(
                "arguments[0].scrollIntoView({block: 'center'})", element
            )
            element.send_keys(Keys.CONTROL, "a", Keys.NULL, str(value), Keys.ENTER)
        except (
            ElementNotInteractableException,
            NoSuchElementException,
            StaleElementReferenceException,
        ):
            return False
        return True

    wait(screen).until(typed)


def click_object(screen, name, *, shift=False):
    """Click the scene object ``name`` where it is hit first."""
    x, y = scene_object_pixel(screen, name)
    actions = ActionChains(screen.selenium)
    if shift:
        actions.key_down(Keys.SHIFT)
    pointer_to(screen, x, y, actions)
    actions.click()
    if shift:
        actions.key_up(Keys.SHIFT)
    actions.perform()


def confirmed(screen, predicate=lambda: True):
    """Wait until the world is confirmed, no object edit is pending and
    ``predicate`` holds."""
    wait(screen).until(
        lambda _: run_in_app(
            lambda: waldoctl.commander.scene.confirmed
            and not ui_state.urdf_scene.object_history.busy
            and predicate()
        )
    )
    wait_for_scene_idle(screen)


def place_shape(screen, kind, x, y, name=None):
    """Place a default ``kind`` keep-out at viewport (x, y) from the menu,
    wait until it is selected and, given ``name``, rename it. Its name."""
    before = {s.name for s in waldoctl.commander.scene.shapes}
    choose(screen, x, y, f"{kind.capitalize()} here")
    placed = poll(
        lambda: next(
            (
                s.name
                for s in run_in_app(lambda: list(waldoctl.commander.scene.shapes))
                if s.name not in before
            ),
            None,
        ),
        10.0,
        f"the placed {kind}",
    )
    wait(screen).until(
        lambda _: run_in_app(
            lambda: ui_state.urdf_scene._selected_objects == [f"shape:{placed}"]
        )
    )
    confirmed(screen)
    if name is not None:
        fill(screen, "object-name", name)
        confirmed(
            screen,
            lambda: any(s.name == name for s in waldoctl.commander.scene.shapes),
        )
        placed = name
    return placed


def create_shape(screen, kind, name):
    """Place a ``kind`` keep-out named ``name`` on the floor, turn it and
    open its resize handles."""
    # An earlier object left selected keeps its handles out over the floor.
    if run_in_app(lambda: bool(ui_state.urdf_scene._selected_objects)):
        click_marked(screen, "object-close")
        wait(screen).until(
            lambda _: not run_in_app(lambda: ui_state.urdf_scene._selected_objects)
        )
        wait_for_scene_idle(screen)
    point = wait(screen).until(lambda _: probe(screen, "floorSpot"))
    place_shape(screen, kind, *point[:2], name)
    object_mode(screen, "Move")
    fill(screen, "object-rz", 30)
    confirmed(screen, lambda: waldoctl.commander.scene.shapes[-1].pose[5] > 0.5)
    object_mode(screen, "Resize")
    wait(screen).until(
        lambda _: scene_js(screen, "return !!S.byName('shape:resize')?.visible")
    )


def object_mode(screen, label):
    """Switch the selected object's handles to ``label``: Move or Resize.
    The inspector is rebuilt as edits land, so the toggle is looked up anew."""

    def clicked(_) -> bool:
        try:
            button = marked_element(screen, "object-transform").find_element(
                By.XPATH, f".//button[.//*[normalize-space()='{label}']]"
            )
            # A short card scrolls, and its other rows can cover the toggle.
            screen.selenium.execute_script(
                "arguments[0].scrollIntoView({block: 'center'})", button
            )
            button.click()
        except (
            ElementClickInterceptedException,
            ElementNotInteractableException,
            NoSuchElementException,
            StaleElementReferenceException,
        ):
            return False
        return True

    wait(screen).until(clicked)


def path_point(screen, command, fraction=0.5):
    """The pixel ``fraction`` of the way along ``command``'s drawn path."""
    points = scene_js(screen, "return S.pathPixels(0, arguments[0])", command)
    assert points, f"command {command} draws no path"
    return points[round((len(points) - 1) * fraction)]


def camera_menu(screen):
    """Open the camera menu and wait for it to finish appearing."""
    click_marked(screen, "camera-menu")
    wait(screen).until(
        lambda _: screen.selenium.execute_script("""
      const menu=document.querySelector('.q-menu');
      return menu && getComputedStyle(menu).opacity === '1' &&
        !menu.getAnimations({subtree:true}).some(a=>a.playState==='running');
    """)
    )


def camera_action(screen, name):
    """Run the camera menu's ``name`` action."""
    camera_menu(screen)
    click_marked(screen, f"camera-{name}")


def camera_settled(screen):
    """Wait until the camera has stopped moving."""
    previous = None
    stable = 0

    def done(_):
        nonlocal previous, stable
        pose = scene_js(screen, "return S.cameraPose()")
        stable = stable + 1 if pose == previous else 0
        previous = pose
        return stable >= 2

    wait(screen).until(done)
    wait_for_scene_idle(screen)


def set_projection(screen, label):
    """Switch the camera to ``label`` projection through its menu."""
    camera_menu(screen)
    marked_element(screen, "camera-projection").find_element(
        By.XPATH, f".//button[.//*[text()='{label}']]"
    ).click()
    wait(screen).until(
        lambda _: scene_js(screen, "return !!S.camera.isOrthographicCamera")
        == (label == "Orthographic")
    )
    click_marked(screen, "camera-menu")
    wait(screen).until(
        lambda _: not screen.selenium.find_elements(By.CSS_SELECTOR, ".q-menu")
    )
    wait_for_scene_idle(screen)


def menu_fits(screen):
    """Wait until the open menu fits without scrolling."""
    wait(screen).until(
        lambda _: screen.selenium.execute_script("""
          const menu=document.querySelector('.q-menu');
          return menu && menu.scrollHeight <= menu.clientHeight + 1;
        """)
    )
