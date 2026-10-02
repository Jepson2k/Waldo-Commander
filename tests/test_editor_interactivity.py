"""The program editor in a real browser: CodeMirror keystrokes, completions,
line decorations, the diff review cluster, skill snippet fields and the
filled controls' computed colours.

The tests share one page through ``class_screen``. Each puts back the
program source, file name and window size it changes.
"""

from typing import TYPE_CHECKING

import pytest
import waldoctl
from nicegui import Client
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from waldoctl.setup import Pose, SetupSnapshot

from tests.helpers.browser_helpers import (
    click_tab,
    ensure_robot_homed,
    focus_editor,
    get_autocomplete_labels,
    js,
    marked_element,
    run_in_app,
    wait_for_autocomplete,
    wait_for_codemirror_ready,
    wait_for_notification,
)
from tests.helpers.browser_session import wait, window_size
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


def _set_editor_content(screen: "Screen", text: str) -> None:
    """Replace the active CodeMirror doc through its own dispatch, so the doc
    and the tab's source stay in step."""
    js(
        screen,
        """
        const text = arguments[0];
        const cm = document.querySelector('.cm-content');
        if (!cm || !cm.cmView || !cm.cmView.view) return;
        const view = cm.cmView.view;
        view.dispatch({
            changes: {from: 0, to: view.state.doc.length, insert: text}
        });
        """,
        text,
    )


def _source() -> str:
    return str(run_in_app(lambda: ui_state.active_textarea.value))


def _set_source(text: str) -> None:
    run_in_app(lambda: setattr(ui_state.active_textarea, "value", text))


# Measures whether the review cluster (with its Approve/Reject buttons) and the
# diff decorations both render, whether the toolbar buttons yielded their spot,
# and whether the editor stays within the panel.
_LAYOUT_JS = """
const panel = document.querySelector('.editor-tab-panel');
const banner = document.querySelector('.pending-edits-banner');
const editor = document.querySelector('.editor-tab-panel .cm-editor');
if (!panel || !editor) return null;
const pr = panel.getBoundingClientRect();
const er = editor.getBoundingClientRect();
const bannerButtons = banner
  ? banner.querySelectorAll('button').length : 0;
const bannerVisible = !!banner
  && banner.getBoundingClientRect().height > 0
  && getComputedStyle(banner).display !== 'none';
const toolbarVisible = [...document.querySelectorAll('.editor-toolbar-btn')]
  .some((el) => el.getBoundingClientRect().height > 0
    && getComputedStyle(el).display !== 'none');
return {
  bannerVisible: bannerVisible,
  bannerButtons: bannerButtons,
  toolbarVisible: toolbarVisible,
  hasDiffDecoration: !!document.querySelector('.cm-edit-remove, .cm-edit-add'),
  editorWithinPanel: er.bottom <= pr.bottom + 2 && er.top >= pr.top - 2,
  bannerAboveEditor: !!banner
    && banner.getBoundingClientRect().bottom <= er.top + 2,
  bannerWithinPanel: !!banner
    && banner.getBoundingClientRect().right <= pr.right + 2,
};
"""

# Records the text of every editor line that carries the flash, whenever it
# lands: the flash lasts 1.5 s.
_WATCH_FLASHES = """
window.__flashed = new Set();
const panel = document.querySelector('.program-panel');
const note = () => panel.querySelectorAll('.cm-line.cm-line-flash')
    .forEach((line) => window.__flashed.add(line.textContent.trim()));
if (window.__flashObserver) window.__flashObserver.disconnect();
window.__flashObserver = new MutationObserver(note);
window.__flashObserver.observe(panel, {subtree: true, childList: true,
                                       attributes: true, attributeFilter: ['class']});
"""


@pytest.fixture(scope="class")
def saved_poses(tmp_path_factory: pytest.TempPathFactory):
    """A saved setup naming the poses a skill call is filled with, in place
    before the class's page is built."""
    directory = tmp_path_factory.mktemp("setups")
    SetupStore(directory).save(
        "bench",
        SetupSnapshot(
            poses={
                "pick": Pose((15, 222, 179, 85, 2, 87)),
                "place": Pose((45, 222, 179, 85, 2, 87)),
            },
        ),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WALDO_SETUP_DIR", str(directory))
        yield


@pytest.mark.browser
@pytest.mark.usefixtures("saved_poses")
class TestEditorInteractivity:
    """Editor tests sharing a single browser session."""

    def test_filled_controls_pair_their_text_colour(
        self, class_screen: "Screen"
    ) -> None:
        """Every filled control renders its glyph in the token the design pairs
        with that fill, as the browser computes it. Quasar's white-on-fill
        default lives in a CSS layer that outranks app rules, so this can only
        be checked on computed styles."""
        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        script = """
            const [icon, token] = arguments;
            const btn = document.evaluate(
                `//button[.//i[text()='${icon}']]`, document, null,
                XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if (!btn) return `no button with icon ${icon}`;
            const probe = document.createElement('span');
            probe.style.color = `var(--wc-${token})`;
            document.body.appendChild(probe);
            const want = getComputedStyle(probe).color;
            probe.remove();
            const got = getComputedStyle(btn.querySelector('i')).color;
            return got === want ? 'ok' : `${icon}: got ${got}, want ${token} ${want}`;
        """
        expectations = {
            "play_arrow": "on-bright",  # run fill
            "dangerous": "on-fill",  # estop fill
            "precision_manufacturing": "on-bright",  # mode-sim fill (simulator on)
            "home": "text",  # control fill
            "stop": "error",  # control fill with a red glyph (hidden until running)
            "fiber_manual_record": (
                "on-fill"
                if "recording"
                in class_screen.selenium.find_element(
                    By.XPATH, "//button[.//i[text()='fiber_manual_record']]"
                ).get_attribute("class")
                else "record"
            ),
        }
        problems = [
            r
            for icon, token in expectations.items()
            if (r := class_screen.selenium.execute_script(script, icon, token)) != "ok"
        ]
        assert not problems, problems

    def test_ctrl_s_saves_the_active_tab_and_reports_a_failed_save(
        self, class_screen: "Screen"
    ) -> None:
        """A real Ctrl+S keystroke goes through CodeMirror's keymap to
        on_save and _save_tab: the tab lands on disk, and a save that fails
        shows its error. The failure is the regression where _save_tab ran in
        a task with no slot stack, so its ui.notify raised and the toast was
        swallowed as "Task exception was never retrieved"."""
        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        active_tab = waldoctl.commander.programs.active
        assert active_tab is not None, "expected an active tab after opening program"

        original_filename = active_tab.filename
        original_file_path = active_tab.file_path
        original_content = active_tab.source
        target_name = "regression_ctrl_s_test.py"
        target_content = "# ctrl-s regression test\n"
        target_path = ui_state.editor_panel.PROGRAM_DIR / target_name
        active_tab.filename = target_name
        # Through CodeMirror, so the editor's doc and tab.source agree: set
        # from Python alone, the editor's empty initial value can clobber it
        # before _save_tab reads it.
        _set_editor_content(class_screen, target_content)

        def _has_target_content(_d: object) -> bool:
            # The save creates the file before writing it.
            try:
                return (
                    target_path.exists() and target_path.read_text() == target_content
                )
            except OSError:
                return False

        try:
            focus_editor(class_screen).send_keys(Keys.CONTROL + "s")
            wait(class_screen).until(_has_target_content)

            # A null byte makes Path.write_text raise.
            active_tab.filename = "regression\x00invalid.py"
            focus_editor(class_screen).send_keys(Keys.CONTROL + "s")
            wait_for_notification(class_screen, "Save failed:", timeout=5.0)
        finally:
            target_path.unlink(missing_ok=True)
            active_tab.filename = original_filename
            active_tab.file_path = original_file_path
            _set_editor_content(class_screen, original_content)

    def test_autocomplete_popup_appears_outside_parens(
        self, class_screen: "Screen"
    ) -> None:
        """Typing `rbt.move` at the top level shows the completion popup with
        `rbt.move_j` and `rbt.move_l`.

        Regression for CM.lintGutter(): its tooltip's null showTooltip provider
        suppresses the popup outside paren contexts.
        """
        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        active_tab = waldoctl.commander.programs.active
        assert active_tab is not None
        original_content = active_tab.source

        # Cleared through dispatch; the typing itself must be real keystrokes
        # so it goes through the keymap and activates completion.
        _set_editor_content(class_screen, "")

        cm_content = focus_editor(class_screen)
        try:
            cm_content.send_keys("rbt.move")

            wait_for_autocomplete(class_screen, timeout=5.0)
            labels = get_autocomplete_labels(class_screen)

            assert any("rbt.move_j" in label for label in labels), (
                f"expected rbt.move_j in completion labels, got: {labels}"
            )
            assert any("rbt.move_l" in label for label in labels), (
                f"expected rbt.move_l in completion labels, got: {labels}"
            )
        finally:
            _set_editor_content(class_screen, original_content)

    def test_capture_pose_adds_and_flashes_line(self, class_screen: "Screen") -> None:
        """Capture pose inserts the robot's pose as a new line, and the editor
        flashes that line."""
        screen = class_screen
        click_tab(screen, "program")
        wait_for_codemirror_ready(screen)
        original = _source()
        # No move to re-teach, so wherever the cursor is, capture inserts.
        program = (
            "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
        )
        _set_source(program)
        try:
            wait(screen).until(
                lambda _: js(
                    screen,
                    "const c = document.querySelector('.program-panel .cm-content');"
                    "return c && c.cmView ? c.cmView.view.state.doc.toString() : null;",
                )
                == program
            )
            js(screen, _WATCH_FLASHES)
            marked_element(screen, "editor-capture-pose").click()
            wait(screen, 5).until(lambda _: _source() != program)
            added = [
                line.strip()
                for line in _source().splitlines()
                if line not in program.splitlines()
            ]
            assert len(added) == 1 and added[0].startswith("rbt.move"), added
            wait(screen, 5).until(
                lambda _: added[0] in js(screen, "return [...window.__flashed]"),
                message=f"the inserted line {added[0]!r} never flashed",
            )
        finally:
            js(screen, "window.__flashObserver?.disconnect()")
            _set_source(original)

    def test_review_controls_and_diff_coexist_without_clipping(
        self, class_screen: "Screen"
    ) -> None:
        """Regression for "I could see either the diff OR the Approve/Reject
        buttons, but not both": the review controls swap in for the toolbar
        in the editor's header row, and the editor stays within its
        fixed-height panel instead of overflowing and being clipped by the
        ancestor ``overflow:hidden`` and bottom mask."""
        screen = class_screen
        # Narrow window: the editor lives in a ~380px overlay panel, so the
        # header must cope with tight widths.
        with window_size(screen, 1000, 900):
            click_tab(screen, "program")
            wait_for_codemirror_ready(screen)
            original = _source()

            def _build_programs():
                p = waldoctl.commander.programs.active
                assert p is not None
                # A tall program + an edit near the bottom: a clipped editor
                # would push the decoration out of the visible panel. Through
                # the editor, like a user: Program.source alone does not push
                # a replacement document into an open CodeMirror.
                ui_state.active_textarea.value = (
                    "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n"
                )
                # A second, very wide tab: the header must shrink the tab strip
                # (it scrolls horizontally) rather than wrap the review cluster
                # onto a second line underneath the CodeMirror.
                second = waldoctl.commander.programs.new(
                    filename="a_very_long_program_filename_that_widens_the_tab_strip_"
                    "far_beyond_any_reasonable_header_width.py"
                )
                return p, second

            p, second = run_in_app(_build_programs)

            try:
                # A long description like an LLM writes: the label must truncate
                # instead of wrapping the cluster or pushing its buttons off-panel.
                run_in_app(
                    lambda: p.edits.propose(
                        "@@ -38,1 +38,1 @@\n-line_37 = 37\n+line_37 = 3737\n",
                        "Home safely before the wave (a blind joint move from a "
                        "folded pose can self-collide)",
                    )
                )
                try:
                    wait(screen, 6).until(
                        lambda _: (i := js(screen, _LAYOUT_JS))
                        and i["bannerVisible"]
                        and i["hasDiffDecoration"]
                    )
                except TimeoutException:
                    pass
                info = js(screen, _LAYOUT_JS)

                assert info is not None, "editor panel never rendered"
                assert info["bannerVisible"] and info["bannerButtons"] >= 2, (
                    f"Approve/Reject review cluster not visible with its buttons: {info}"
                )
                assert not info["toolbarVisible"], (
                    f"toolbar buttons must yield to the review cluster while an "
                    f"edit is pending: {info}"
                )
                assert info["hasDiffDecoration"], (
                    f"diff decorations not rendered: {info}"
                )
                assert info["editorWithinPanel"], (
                    f"editor overflows/clips the panel — the 'diff OR buttons' bug: {info}"
                )
                assert info["bannerAboveEditor"], (
                    f"review cluster wrapped below the header and is painted under "
                    f"the editor: {info}"
                )
                assert info["bannerWithinPanel"], (
                    f"review cluster overflows the panel — Approve/Reject "
                    f"unreachable: {info}"
                )
            finally:

                def _cleanup():
                    for e in list(p.edits.pending):
                        p.edits.reject(e.id)
                    waldoctl.commander.programs.close(second.id)
                    ui_state.textareas_by_tab[p.id].value = original

                run_in_app(_cleanup)

    def test_skill_fields_tab_in_order_and_stay_live_through_a_strip_write(
        self, class_screen: "Screen"
    ) -> None:
        """A skill goes into the program as its call with the arguments as
        fields: Tab moves between them, a write from the strip above the code
        keeps them live, and the strip leaves the code and the scene usable."""
        screen = class_screen
        driver = screen.selenium
        # The preview the skill draws mirrors the controller's unhomed gate.
        ensure_robot_homed()

        def element_id(marker: str) -> int | None:
            def find():
                client = Client.instances[ui_state.active_client_id]
                return next(
                    (e.id for e in client.elements.values() if marker in e._markers),
                    None,
                )

            return run_in_app(find)

        def click(marker: str) -> None:
            target = wait(screen).until(
                lambda d: (
                    (found := element_id(marker)) is not None
                    and (el := d.find_element(By.ID, f"c{found}")).is_displayed()
                    and el
                )
            )
            target.click()

        def preview_objects() -> int:
            scene = ui_state.urdf_scene
            assert scene is not None
            return len(scene._skill_preview_objects)

        def selected() -> str:
            """The text of the editor's selection, where a snippet field is selected."""
            return driver.execute_script(
                "const s=getElement(arguments[0]).editor.state;"
                "return s.sliceDoc(s.selection.main.from, s.selection.main.to);",
                run_in_app(lambda: ui_state.active_textarea.id),
            )

        def strip():
            return ui_state.editor_panel.skill_strip(
                waldoctl.commander.programs.active_id
            )

        def strip_field() -> str | None:
            return run_in_app(lambda: strip()._field if strip() is not None else None)

        with window_size(screen, 1366, 768):
            click("tab-program")
            wait_for_codemirror_ready(screen)
            original = _source()
            try:
                click("editor-commands-btn")
                click("editor-skills-menu")
                # A skill's diagram is its label in the menu; one that failed
                # to load is an empty square beside a word.
                loaded = wait(screen).until(
                    lambda d: d.execute_script(
                        "const imgs=[...document.querySelectorAll('.q-menu .skill-menu-icon img')];"
                        "return imgs.length && imgs.every(i => i.complete && i.naturalWidth > 0);"
                    )
                )
                assert loaded
                click("editor-skill-waldo.approach")

                # The call goes in with its first field selected and the editor
                # focused, and the saved pose it names has a motion to draw.
                wait(screen).until(lambda _: selected() == 'setup.resolve("pick")')
                wait(screen, 15).until(lambda _: run_in_app(preview_objects) > 0)
                layout = driver.execute_script(
                    "const strip=[...document.querySelectorAll('.skill-strip')].find(e => e.offsetParent);"
                    "const r=strip.getBoundingClientRect();"
                    "const code=strip.parentElement.querySelector('.cm-editor').getBoundingClientRect();"
                    "const canvas=document.querySelector('canvas').getBoundingClientRect();"
                    "const hit=document.elementFromPoint(canvas.x+canvas.width*0.6, canvas.y+canvas.height/2);"
                    "return {content:strip.scrollWidth, width:strip.clientWidth, bottom:r.bottom,"
                    "codeTop:code.top, code:code.height, scene: hit && hit.tagName};"
                )
                assert layout["content"] <= layout["width"] + 1, layout
                assert layout["codeTop"] >= layout["bottom"] - 1, layout
                assert layout["code"] > 100, (
                    "the code stays in view under the strip",
                    layout,
                )
                # Nothing covers the scene: where no panel is, it is still the scene.
                assert layout["scene"] == "CANVAS", layout

                # Tab and Shift+Tab move between the fields in the order of the
                # signature.
                ActionChains(driver).send_keys(Keys.TAB).perform()
                wait(screen, 5).until(lambda _: selected() == "30.0")
                ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.TAB).key_up(
                    Keys.SHIFT
                ).perform()
                wait(screen, 5).until(lambda _: selected() == 'setup.resolve("pick")')
                wait(screen, 5).until(lambda _: strip_field() == "target")

                # Choosing a pose in the strip writes only that field from the
                # server: the snippet stays live, so Tab still moves on to the
                # next field.
                def choose_place() -> None:
                    with Client.instances[ui_state.active_client_id]:
                        strip().write_field('setup.resolve("place")')

                run_in_app(choose_place)
                wait(screen, 5).until(lambda _: selected() == 'setup.resolve("place")')
                ActionChains(driver).send_keys(Keys.TAB).perform()
                wait(screen, 5).until(lambda _: selected() == "30.0")
                ActionChains(driver).send_keys(Keys.TAB).perform()
                wait(screen, 5).until(lambda _: selected() == "0.2")
            finally:
                _set_source(original)
