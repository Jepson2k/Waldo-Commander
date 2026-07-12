"""Browser-level layout check for the LLM diff overlay.

Regression for "I could see either the diff OR the Approve/Reject buttons, but
not both": the pending-edits banner and the in-editor diff decorations must be
visible together in one panel — the CodeMirror editor must flex-shrink to make
room for the banner instead of overflowing the fixed-height panel and being
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

# Measures whether the banner (with its Approve/Reject buttons) and the diff
# decorations both render, and whether the editor stays within the panel.
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
return {
  bannerVisible: bannerVisible,
  bannerButtons: bannerButtons,
  hasDiffDecoration: !!document.querySelector('.cm-edit-remove, .cm-edit-add'),
  editorWithinPanel: er.bottom <= pr.bottom + 2 && er.top >= pr.top - 2,
};
"""


@pytest.mark.browser
def test_banner_and_diff_coexist_without_clipping(screen) -> None:
    screen.open("/")
    dismiss_dialogs(screen)
    click_tab(screen, "program")
    wait_for_codemirror_ready(screen)

    p = waldoctl.commander.programs.active
    assert p is not None
    # A tall program + an edit near the bottom: a clipped editor would push the
    # decoration out of the visible panel.
    p.source = "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n"

    try:
        p.edits.propose("@@ -38,1 +38,1 @@\n-line_37 = 37\n+line_37 = 3737\n", "tweak")

        deadline = time.time() + 6.0
        info = None
        while time.time() < deadline:
            info = screen.selenium.execute_script(_LAYOUT_JS)
            if info and info.get("bannerVisible") and info.get("hasDiffDecoration"):
                break
            time.sleep(0.1)

        assert info is not None, "editor panel never rendered"
        assert info["bannerVisible"] and info["bannerButtons"] >= 2, (
            f"Approve/Reject banner not visible with its buttons: {info}"
        )
        assert info["hasDiffDecoration"], f"diff decorations not rendered: {info}"
        assert info["editorWithinPanel"], (
            f"editor overflows/clips the panel — the 'diff OR buttons' bug: {info}"
        )
    finally:
        for e in list(p.edits.pending):
            p.edits.reject(e.id)
