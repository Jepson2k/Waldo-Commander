"""Browser-level layout check for the LLM diff overlay.

Regression for "I could see either the diff OR the Approve/Reject buttons, but
not both": the review controls and the in-editor diff decorations must be
visible together. The controls live in the editor's header row (swapping in
for the toolbar buttons while an edit is pending), and the CodeMirror editor
must stay within its fixed-height panel instead of overflowing and being
clipped by the ancestor ``overflow:hidden`` + bottom mask.
"""

import time

import pytest

import waldoctl
from tests.helpers.browser_helpers import (
    click_tab,
    dismiss_dialogs,
    wait_for_codemirror_ready,
)

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


@pytest.mark.browser
def test_review_controls_and_diff_coexist_without_clipping(screen) -> None:
    screen.open("/")
    # Narrow window: in the app the editor lives in a ~380px overlay panel,
    # so the header must cope with tight widths.
    screen.selenium.set_window_size(760, 900)
    dismiss_dialogs(screen)
    click_tab(screen, "program")
    wait_for_codemirror_ready(screen)

    p = waldoctl.commander.programs.active
    assert p is not None
    # A tall program + an edit near the bottom: a clipped editor would push the
    # decoration out of the visible panel.
    p.source = "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n"
    # A second, very wide tab: the header must shrink the tab strip (it
    # scrolls horizontally) rather than wrap the review cluster onto a second
    # line underneath the CodeMirror.
    second = waldoctl.commander.programs.new(
        filename="a_very_long_program_filename_that_widens_the_tab_strip_far_beyond"
        "_any_reasonable_header_width.py"
    )

    try:
        # A long description like an LLM writes: the label must truncate
        # instead of wrapping the cluster or pushing its buttons off-panel.
        p.edits.propose(
            "@@ -38,1 +38,1 @@\n-line_37 = 37\n+line_37 = 3737\n",
            "Home safely before the wave (a blind joint move from a folded "
            "pose can self-collide)",
        )

        deadline = time.time() + 6.0
        info = None
        while time.time() < deadline:
            info = screen.selenium.execute_script(_LAYOUT_JS)
            if info and info.get("bannerVisible") and info.get("hasDiffDecoration"):
                break
            time.sleep(0.1)

        assert info is not None, "editor panel never rendered"
        assert info["bannerVisible"] and info["bannerButtons"] >= 2, (
            f"Approve/Reject review cluster not visible with its buttons: {info}"
        )
        assert not info["toolbarVisible"], (
            f"toolbar buttons must yield to the review cluster while an edit "
            f"is pending: {info}"
        )
        assert info["hasDiffDecoration"], f"diff decorations not rendered: {info}"
        assert info["editorWithinPanel"], (
            f"editor overflows/clips the panel — the 'diff OR buttons' bug: {info}"
        )
        assert info["bannerAboveEditor"], (
            f"review cluster wrapped below the header and is painted under "
            f"the editor: {info}"
        )
        assert info["bannerWithinPanel"], (
            f"review cluster overflows the panel — Approve/Reject unreachable: {info}"
        )
    finally:
        for e in list(p.edits.pending):
            p.edits.reject(e.id)
        waldoctl.commander.programs.close(second.id)
