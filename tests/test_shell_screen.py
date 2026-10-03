"""The app shell in a real browser: the joint dials, the scene framing, the
AI-control glow, and the status footer, which stays one row on a desktop and
keeps every reading below the existing controls on narrow screens.

The tests share one page. Each puts back what it changes (pose, tool, lease,
window size); the controls-only test reloads the page in a phone viewport, so
it runs last.
"""

import asyncio
import math

import pytest
import waldoctl
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
)
from nicegui import Client, core

from tests.helpers.browser_helpers import (
    click_tab,
    close_panel,
    js,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import no_visible, wait, window_size
from tests.helpers.wait import JOG_SAFE_POSE_DEG, screen_wait_for_scene_ready

from waldo_commander.components.joint_dial import DIAL_RADIUS, dial_angle
from waldo_commander.services.control_lease import (
    BROWSER,
    MCP,
    browser_try_acquire,
    control_lease,
)
from waldo_commander.state import ui_state


def _clicked(element) -> bool:
    try:
        element.click()
    except (ElementNotInteractableException, ElementClickInterceptedException):
        return False
    return True


MEASURE = """
    const footer = document.querySelector('.status-footer');
    const f = footer.getBoundingClientRect();
    const well = footer.querySelector('.pose-well').getBoundingClientRect();
    const children = [...footer.querySelectorAll(':scope > *')]
        .filter(e => e.offsetParent !== null)
        .map(e => e.getBoundingClientRect());
    return {height: f.height, top: f.top, bottom: f.bottom, left: f.left, right: f.right,
            viewport: innerHeight, viewportWidth: innerWidth,
            wellRight: well.right, wellLeft: well.left, wellTop: well.top, wellBottom: well.bottom,
            lowest: Math.max(...children.map(r => r.bottom)),
            highest: Math.min(...children.map(r => r.top)),
            dots: footer.querySelectorAll('.io-dot').length};
"""

LAUNCHERS = """
    const f = document.querySelector('.status-footer').getBoundingClientRect();
    const tool = document.querySelector('.footer-tool');
    return {footer: {left: f.left, right: f.right, top: f.top, bottom: f.bottom},
            tool: !!tool && tool.offsetParent !== null,
            launchers: arguments[0].map(id => {
                const r = document.getElementById(id).getBoundingClientRect();
                return {left: r.left, right: r.right, width: r.width};
            })};
"""

VIEW = """
    const cam = window.SceneFraming && SceneFraming.camera();
    if (!cam) return null;
    const el = document.querySelector('.nicegui-scene');
    const footer = document.querySelector('.status-footer').getBoundingClientRect();
    return {enabled: !!(cam.view && cam.view.enabled),
            fullWidth: cam.view ? cam.view.fullWidth : null,
            fullHeight: cam.view ? cam.view.fullHeight : null,
            offsetY: cam.view ? cam.view.offsetY : null,
            aspect: cam.aspect,
            width: el.clientWidth, height: el.clientHeight,
            columnRight: PanelResize.layout().columnRight,
            footerCover: Math.round(innerHeight - footer.top)};
"""

NO_TOOLTIP = "return !document.querySelector('.q-tooltip')"


def _show_lines(count: int | None) -> None:
    """Rebuild the dots for *count* lines each way and hold them there: the
    status loop re-reads the robot's real line count every tick. None hands
    the footer back to the status loop."""

    def apply():
        footer = ui_state.readout_panel
        if count is None:
            footer.__dict__.pop("update_conn_io", None)
        else:
            io = waldoctl.commander.status.io
            io.inputs = [0] * count
            io.outputs = [0] * count
        with Client.instances[ui_state.active_client_id]:
            type(footer).update_conn_io(footer)
        if count is not None:
            footer.update_conn_io = lambda: None

    run_in_app(apply)


def _measure_with(screen, dots: int) -> dict:
    return wait(screen).until(
        lambda _: (m := js(screen, MEASURE))["dots"] == dots and m
    )


def _select_tool(key: str) -> None:
    async def select() -> None:
        client = waldoctl.commander.client
        assert await client.wait_command(await client.select_tool(key), timeout=10)

    asyncio.run_coroutine_threadsafe(select(), core.loop).result(20)


def _teleport(pose: list[float]) -> None:
    async def teleport() -> None:
        await waldoctl.commander.client.teleport(pose)

    asyncio.run_coroutine_threadsafe(teleport(), core.loop).result(15)


def _launchers(screen, markers: list[str]) -> dict:
    ids = [marked_element(screen, m).get_attribute("id") for m in markers]
    return js(screen, LAUNCHERS, ids)


def _settings_open() -> bool:
    return run_in_app(lambda: ui_state.settings_content.dialog.value)


def _view(screen) -> dict:
    view = js(screen, VIEW)
    assert view is not None, "scene-framing.js is not attached to the scene"
    return view


def _wait_view(screen, predicate) -> dict:
    return wait(screen).until(lambda _: (v := _view(screen)) and predicate(v) and v)


@pytest.mark.browser
class TestShellLayout:
    def test_the_joint_tab_is_as_tall_as_its_dials(self, class_screen) -> None:
        screen_wait_for_scene_ready(class_screen, timeout_s=40.0)
        sizes = wait(class_screen).until(
            lambda _: js(
                class_screen,
                """
                const panels = document.querySelector('.cp-jog-panels').getBoundingClientRect();
                const cells = [...document.querySelectorAll('.joint-dial-cell')].map(c => c.getBoundingClientRect());
                if (!cells.length) return null;
                return {panelsHeight: panels.height,
                        dialsHeight: Math.max(...cells.map(c => c.bottom)) - panels.top};
                """,
            )
        )
        assert sizes["panelsHeight"] <= sizes["dialsHeight"] + 8, sizes

    def test_a_dial_shows_a_move_made_while_the_cartesian_tab_was_open(
        self, class_screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        lo, hi = run_in_app(lambda: ui_state.control_panel._get_joint_limits(0))
        pose = list(JOG_SAFE_POSE_DEG)

        def teleport_j1(deg: float) -> None:
            pose[0] = deg
            _teleport(pose)
            wait(screen).until(
                lambda _: abs(waldoctl.commander.status.joints.angles.deg[0] - deg)
                < 0.5,
                message=f"J1 never reached {deg}°",
            )

        def j1_knob_at(deg: float) -> bool:
            knob = js(
                screen,
                """
                const k = document.querySelector('.joint-dial-cell .dial-knob');
                return k && [+k.getAttribute('cx'), +k.getAttribute('cy')];
                """,
            )
            return (
                bool(knob)
                and math.dist(knob, dial_angle(lo, hi, deg, DIAL_RADIUS)[1]) < 0.5
            )

        teleport_j1(85.0)
        wait(screen).until(lambda _: j1_knob_at(85.0))

        marked_element(screen, "tab-cartesian").click()
        # Unmounted, so the Joint tab mounts it again from its props.
        wait(screen).until(
            lambda _: not js(
                screen, "return !!document.querySelector('.joint-dial-cell')"
            )
        )
        teleport_j1(40.0)
        marked_element(screen, "tab-joint").click()
        wait(screen).until(
            lambda _: j1_knob_at(40.0),
            message="J1 moved to 40° while the Cartesian tab was open, "
            "but its dial does not show it on the Joint tab",
        )

    def test_idle_dials_hide_their_caps_even_at_a_limit(self, class_screen) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        lo, _hi = run_in_app(lambda: ui_state.control_panel._get_joint_limits(2))
        pose = list(JOG_SAFE_POSE_DEG)
        pose[2] = lo
        try:
            _teleport(pose)
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 5, "y": 5}
            )
            caps = wait(screen).until(
                lambda _: (
                    r := js(
                        screen,
                        """
                        const caps = [...document.querySelectorAll('.joint-cap')];
                        const shown = c => getComputedStyle(c).visibility !== 'hidden'
                            && getComputedStyle(c).opacity !== '0';
                        return {disabled: caps.filter(c => c.classList.contains('disabled')).length,
                                shown: caps.filter(shown).length};
                        """,
                    )
                )
                and r["disabled"] > 0
                and r
            )
            assert caps["shown"] == 0, f"an idle dial shows its caps: {caps}"
        finally:
            _teleport(list(JOG_SAFE_POSE_DEG))

    def test_view_offset_follows_the_column_and_footer(self, class_screen) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)

        # Nothing but the footer covers the view: offset up by its height, no shift sideways.
        closed = _wait_view(screen, lambda v: v["enabled"] and v["columnRight"] == 0)
        assert closed["fullWidth"] == closed["width"], closed
        assert closed["offsetY"] == closed["footerCover"] > 0, closed
        assert closed["fullHeight"] == closed["height"] + closed["footerCover"], closed

        click_tab(screen, "program")
        opened = _wait_view(
            screen, lambda v: v["columnRight"] > 0 and v["fullWidth"] > v["width"]
        )
        assert opened["fullWidth"] == opened["width"] + opened["columnRight"], opened
        assert opened["offsetY"] == opened["footerCover"], opened
        assert (
            abs(opened["aspect"] - opened["fullWidth"] / opened["fullHeight"]) < 1e-6
        ), opened

        # A window resize lets the scene reset its aspect; the offset comes back.
        with window_size(screen, 1200, 700):
            resized = _wait_view(
                screen,
                lambda v: v["width"] != opened["width"]
                and v["enabled"]
                and v["fullWidth"] == v["width"] + v["columnRight"],
            )
            assert (
                abs(resized["aspect"] - resized["fullWidth"] / resized["fullHeight"])
                < 1e-6
            ), resized

            close_panel(screen, "program-panel")
            reclosed = _wait_view(
                screen, lambda v: v["columnRight"] == 0 and v["fullWidth"] == v["width"]
            )
            assert reclosed["offsetY"] == reclosed["footerCover"], reclosed

    def test_the_ai_driving_glow_covers_the_viewport(self, class_screen) -> None:
        """An ancestor with backdrop-filter (the overlay card) turns
        position:fixed into card-relative, shrinking the screen-edge glow to
        the card. Which classes and buttons show for which holder is the
        ``user`` tests' business; only the browser can lay the glow out."""
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        try:
            control_lease.seize(MCP, "screen-mcp", "MCP session screen-m")
            # The 1 Hz active-tab loop refreshes the indicator.
            for selector in (".control-lease-glow", ".btn-take-control"):
                wait(screen, 5).until(
                    lambda _, s=selector: not no_visible(screen, s),
                    message=f"{selector} did not show while an AI holds control",
                )
            rect = js(
                screen,
                "const r = document.querySelector('.control-lease-glow')"
                ".getBoundingClientRect();"
                "return [r.left, r.top, r.width, r.height,"
                " window.innerWidth, window.innerHeight];",
            )
            left, top, width, height, vw, vh = rect
            assert (left, top) == (0, 0) and width >= vw - 1 and height >= vh - 1, (
                f"glow does not cover the viewport: {rect}"
            )
        finally:
            browser_try_acquire(ui_state.active_client_id)
        assert control_lease.held_by(BROWSER, ui_state.active_client_id)
        wait(screen, 5).until(lambda _: no_visible(screen, ".control-lease-glow"))

    def test_many_io_lines_keep_the_footer_one_row(self, class_screen) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        with window_size(screen, 1366, 768):
            before = _measure_with(screen, 4)
            assert before["height"] <= 29, before

            try:
                _show_lines(12)
                many = _measure_with(screen, 24)
            finally:
                _show_lines(None)
            assert many["height"] <= 29, many
            assert (
                many["highest"] >= many["top"] - 1
                and many["lowest"] <= many["bottom"] + 1
            ), many
            assert many["bottom"] <= many["viewport"], many
            # The pose well is whole and inside the viewport, dots or no dots.
            assert (
                many["wellLeft"] >= many["left"] and many["wellRight"] <= many["right"]
            ), many
            assert many["wellRight"] <= many["viewportWidth"], many
            assert (
                many["wellTop"] >= many["top"] and many["wellBottom"] <= many["bottom"]
            ), many

            restored = _measure_with(screen, 4)
            assert abs(restored["height"] - before["height"]) < 1, (restored, before)

            # Narrower windows give up the pose before the Diagnostics and Log
            # launchers, even with a tool named.
            try:
                _select_tool("SSG-48")
                for width in (1100, 1000):
                    with viewport(screen, width, 768):
                        shown = wait(screen).until(
                            lambda _: (
                                m := _launchers(screen, ["footer-events", "footer-log"])
                            )["tool"]
                            and m["footer"]["right"] <= width
                            and m
                        )
                        for launcher in shown["launchers"]:
                            assert launcher["width"] > 0, (width, shown)
                            assert launcher["left"] >= shown["footer"]["left"], (
                                width,
                                shown,
                            )
                            assert launcher["right"] <= shown["footer"]["right"], (
                                width,
                                shown,
                            )
                        assert js(screen, MEASURE)["height"] <= 29, width
            finally:
                _select_tool("NONE")

    def test_bottom_panel_pushes_the_column_up_and_stays_clear_of_the_control_panel(
        self, class_screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        geometry = """
            const box = s => { const e = document.querySelector(s); if (!e || e.offsetParent === null) return null;
                               const b = e.getBoundingClientRect(); return {left: b.left, right: b.right, top: b.top, bottom: b.bottom}; };
            return {panel: box('.bottom-panel'), column: box('.program-panel'), control: box('.overlay-br'),
                    viewport: innerHeight, framedBottom: SceneFraming.getInset().bottom};
        """
        with window_size(screen, 1366, 768):
            click_tab(screen, "program")
            click_tab(screen, "diagnostics")
            opened = wait(screen).until(
                lambda _: (g := js(screen, geometry))["panel"]
                and g["column"]["bottom"] <= g["panel"]["top"] - 11
                and g
            )
            panel, column, control = (
                opened["panel"],
                opened["column"],
                opened["control"],
            )
            assert abs(panel["left"] - column["left"]) <= 1, opened
            assert panel["right"] <= control["left"] - 11, opened
            assert opened["framedBottom"] == round(opened["viewport"] - panel["top"]), (
                opened
            )
            wrapped = js(
                screen,
                """
                return [...document.querySelectorAll('.bottom-panel .font-mono')]
                    .filter(e => e.offsetParent !== null && e.textContent.trim())
                    .filter(e => e.getClientRects().length > 1
                        || e.offsetHeight > 1.5 * parseFloat(getComputedStyle(e).lineHeight))
                    .map(e => e.textContent);
                """,
            )
            assert wrapped == [], f"diagnostics values wrap: {wrapped}"

            close_panel(screen, "bottom-panel")
            closed = wait(screen).until(
                lambda _: (g := js(screen, geometry))["panel"] is None
                and g["column"]["bottom"] > opened["column"]["bottom"] + 300
                and g
            )
            assert closed["framedBottom"] < opened["framedBottom"], closed

            # On a short desktop the bottom panel gives way, so the column keeps
            # room for its playbar and the Stop on it.
            playbar = """
                const box = s => { const e = document.querySelector(s); if (!e || e.offsetParent === null) return null;
                                   const b = e.getBoundingClientRect(); return {top: b.top, bottom: b.bottom}; };
                return {panel: box('.bottom-panel'), column: box('.top-panels-container'),
                        playbar: box('.program-panel .bottom-playback-bar'), viewport: innerHeight};
            """
            with viewport(screen, 1000, 480):
                click_tab(screen, "diagnostics")
                kiosk = wait(screen).until(
                    lambda _: (g := js(screen, playbar))["panel"]
                    and g["column"]["bottom"] <= g["panel"]["top"] - 11
                    and g
                )
                column, bar = kiosk["column"], kiosk["playbar"]
                assert column["bottom"] - column["top"] >= 199, kiosk
                assert bar is not None, kiosk
                assert (
                    column["top"] <= bar["top"] and bar["bottom"] <= column["bottom"]
                ), kiosk
                assert bar["bottom"] <= kiosk["viewport"], kiosk
                close_panel(screen, "bottom-panel")
            close_panel(screen, "program-panel")

    def test_the_bottom_panel_reopens_on_its_tab_after_a_reload(
        self, class_screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        wait(screen).until(
            lambda _: js(screen, "return !!window.PanelResize?.isAppReady()")
        )
        saved = (
            "return JSON.parse(localStorage.getItem('parol_active_tabs') || '{}').panel"
        )

        click_tab(screen, "log")
        wait(screen, 5).until(lambda _: js(screen, saved) == "log")

        screen.selenium.refresh()
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        wait(screen).until(
            lambda _: run_in_app(
                lambda: ui_state.bottom_panel.visible
                and ui_state.bottom_panel.tabs.value == "log"
            )
        )
        wait(screen).until(
            lambda _: marked_element(screen, "response-log").is_displayed()
        )

        close_panel(screen, "bottom-panel")
        wait(screen, 5).until(lambda _: js(screen, saved) is None)

    def test_controls_only_layout_keeps_all_footer_readings_and_restores_the_editor(
        self, class_screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=60.0)
        measure = """
            const visible = e => !!e && !!e.getClientRects().length && e.offsetParent !== null;
            const box = e => { const r=e.getBoundingClientRect();
                return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height}; };
            const footer=document.querySelector('.status-footer');
            const control=document.querySelector('.overlay-br');
            const readings=[...footer.querySelectorAll('.pose-cell')];
            const robotName=footer.querySelector('.readout-robot-name');
            return {footer:box(footer), control:box(control), id:control.id,
                robotClipped:robotName.scrollWidth > robotName.clientWidth,
                editor:visible(document.querySelector('.program-panel')),
                scene:visible(document.querySelector('.nicegui-scene')),
                readings:readings.map(e=>({visible:visible(e), ...box(e)})),
                dials:[...control.querySelectorAll('.joint-dial')].map(box),
                width:innerWidth, height:innerHeight};
        """

        def rest_the_pointer() -> None:
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
            )
            wait(screen, 5).until(lambda _: js(screen, NO_TOOLTIP))

        with window_size(screen, 1366, 900):
            click_tab(screen, "program")
            source = "# Keep this unsaved program across phone orientation changes.\n"
            run_in_app(lambda: setattr(ui_state.active_textarea, "value", source))
            panel_id = js(screen, "return document.querySelector('.overlay-br').id")
            try:
                for width, height in ((390, 844), (667, 375), (844, 390), (568, 320)):
                    with viewport(screen, width, height, mobile=True):
                        layout = wait(screen).until(
                            lambda _: (g := js(screen, measure))["width"] == width and g
                        )
                        assert not layout["editor"] and not layout["scene"], layout
                        assert len(layout["readings"]) == 7, layout
                        assert not layout["robotClipped"], layout
                        f, c = layout["footer"], layout["control"]
                        assert c["top"] >= 0 and c["bottom"] <= f["top"] + 1, layout
                        assert (
                            f["bottom"] <= height
                            and f["left"] >= 0
                            and f["right"] <= width
                        ), layout
                        for reading in layout["readings"]:
                            assert reading["visible"], layout
                            assert (
                                f["left"]
                                <= reading["left"]
                                < reading["right"]
                                <= f["right"]
                            ), layout
                            assert (
                                f["top"]
                                <= reading["top"]
                                < reading["bottom"]
                                <= f["bottom"]
                            ), layout
                        assert layout["id"] == panel_id, "resizing rebuilt the controls"
                        assert len(layout["dials"]) == 6, layout
                        assert (
                            max(d["top"] for d in layout["dials"])
                            - min(d["top"] for d in layout["dials"])
                            < 1
                        ), "joint controls must remain in their existing single row"
                        rest_the_pointer()
                        if (width, height) == (390, 844):
                            # The rail and its gear are hidden on a phone; the
                            # footer has one.
                            gear = marked_element(screen, "footer-settings")
                            assert gear.is_displayed()
                            placed = _launchers(screen, ["footer-settings"])
                            assert (
                                placed["launchers"][0]["right"]
                                <= placed["footer"]["right"]
                            ), placed
                            gear.click()
                            wait(screen).until(lambda _: _settings_open())
                            # A click while the dialog is still sliding in can
                            # be dropped; close it until it is closed.
                            wait(screen).until(
                                lambda _: not _settings_open()
                                or (
                                    _clicked(marked_element(screen, "settings-close"))
                                    and not _settings_open()
                                )
                            )
                            wait(screen, 5).until(
                                lambda _: no_visible(screen, ".q-dialog__backdrop")
                            )
                        for name in ("diagnostics", "log"):
                            click_tab(screen, name)
                            panel = js(
                                screen,
                                "return document.querySelector('.bottom-panel').getBoundingClientRect().toJSON()",
                            )
                            assert 0 <= panel["left"] < panel["right"] <= width, panel
                            assert 0 <= panel["top"] < panel["bottom"] <= f["top"], (
                                panel
                            )
                            close_panel(screen, "bottom-panel")
                with viewport(screen, 1366, 480):
                    restored = wait(screen).until(
                        lambda _: (g := js(screen, measure))["editor"]
                        and g["scene"]
                        and g
                    )
                    assert restored["footer"]["height"] <= 29, restored
                    assert restored["id"] == panel_id
                    assert run_in_app(lambda: ui_state.active_textarea.value) == source
                with viewport(screen, 390, 844, mobile=True):
                    screen.selenium.refresh()
                    screen_wait_for_scene_ready(screen, timeout_s=60.0)
                    fresh = wait(screen).until(
                        lambda _: (g := js(screen, measure))["control"]["height"] > 0
                        and all(r["visible"] for r in g["readings"])
                        and g
                    )
                    assert not fresh["scene"] and not fresh["editor"], fresh
                    assert fresh["control"]["bottom"] <= fresh["footer"]["top"] + 1, (
                        fresh
                    )
                    _select_tool("SSG-48")
                    wait(screen).until(
                        lambda _: js(
                            screen,
                            "return document.querySelector('.footer-tool').textContent.includes('SSG')",
                        )
                    )
                    with viewport(screen, 568, 320, mobile=True):
                        small = wait(screen).until(
                            lambda _: (g := js(screen, measure))["width"] == 568
                            and g["control"]["top"] >= 0
                            and g["control"]["bottom"] <= g["footer"]["top"] + 1
                            and g
                        )
                        for reading in small["readings"]:
                            assert reading["visible"], small
                        log = marked_element(screen, "footer-log")
                        js(
                            screen,
                            "arguments[0].scrollIntoView({block:'nearest'})",
                            log,
                        )
                        log.click()
                        wait(screen).until(
                            lambda _: marked_element(
                                screen, "response-log"
                            ).is_displayed()
                        )
                        close_panel(screen, "bottom-panel")
                        rest_the_pointer()
            finally:
                _select_tool("NONE")
