"""Tests for file operations in the editor.

Tests save, load, download, and upload operations using the simulated user fixture.
Uses the tree-based save/open dialogs.

Button markers:
- editor-open-btn: Opens the Open dialog
- editor-save-btn: Opens the Save dialog
- editor-new-tab-btn: Create new tab

Dialog markers:
- open-file-tree: Tree in Open dialog
- open-confirm-btn: Open button in Open dialog
- open-upload: Upload button in Open dialog
- save-file-tree: Tree in Save dialog
- save-confirm-btn: Save button in Save dialog
- save-download-btn: Download button in Save dialog
"""

import asyncio
from typing import TYPE_CHECKING

import pytest
from nicegui.testing.user_interaction import UserInteraction

from tests.helpers.wait import wait_for_app_ready, wait_until

if TYPE_CHECKING:
    from nicegui.testing import User


def _newest(user: "User", marker: str) -> UserInteraction:
    """The marked element of the dialog opened last; a closed dialog keeps its
    elements, and a plain find would click the first one ever built."""
    newest = max(user.find(marker=marker).elements, key=lambda e: e.id)
    return UserInteraction(user, {newest}, None)


@pytest.mark.integration
async def test_new_tab_save_download_and_open_dialogs(user: "User") -> None:
    """The editor's file buttons: New adds a tab; the Save dialog shows the
    tree, writes the tab to PROGRAM_DIR and offers a download; the Open
    dialog shows the tree and the upload."""
    import waldoctl

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    editor = ui_state.editor_panel
    assert editor is not None

    initial_tab_count = len(waldoctl.commander.programs.items)
    user.find(marker="editor-new-tab-btn").click()
    await asyncio.sleep(0)
    assert len(waldoctl.commander.programs.items) == initial_tab_count + 1

    active_tab = waldoctl.commander.programs.active
    assert active_tab is not None
    test_content = "# Save test\nprint('saved')\n"
    test_filename = "test_save_direct.py"
    active_tab.source = test_content
    # The Save dialog's filename field starts from the tab's own name.
    active_tab.filename = test_filename
    test_file = editor.PROGRAM_DIR / test_filename
    try:
        user.find(marker="editor-save-btn").click()
        await asyncio.sleep(0)
        await user.should_see(marker="save-file-tree")
        await user.should_see(marker="save-confirm-btn")
        await user.should_see(marker="save-download-btn")
        _newest(user, "save-confirm-btn").click()
        assert await wait_until(test_file.exists), f"File should exist at {test_file}"
        assert test_file.read_text(encoding="utf-8") == test_content

        user.find(marker="editor-save-btn").click()
        await asyncio.sleep(0)
        _newest(user, "save-download-btn").click()
        response = await user.download.next(timeout=2.0)
        assert response.status_code == 200
        assert len(response.content) > 0
    finally:
        if test_file.exists():
            test_file.unlink()

    user.find(marker="editor-open-btn").click()
    await asyncio.sleep(0)
    await user.should_see(marker="open-file-tree")
    await user.should_see(marker="open-confirm-btn")
    await user.should_see(marker="open-upload")


def test_build_file_tree_ids_are_unique_for_same_named_files_in_nested_dirs(tmp_path):
    """Regression: pre-fix, same-named files in different subdirs collided in
    ui.tree's selection model because file IDs used ``item.name``. Fix uses
    ``str(item.relative_to(base))`` to guarantee uniqueness."""
    from waldo_commander.components.file_operations import FileOperationsMixin

    (tmp_path / "home.py").write_text("")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "home.py").write_text("")

    nodes = FileOperationsMixin._build_file_tree(tmp_path, tmp_path)

    def file_ids(node_list):
        out = []
        for n in node_list:
            if n.get("children"):
                out.extend(file_ids(n["children"]))
            else:
                out.append(n["id"])
        return out

    ids = file_ids(nodes)
    assert len(ids) == len(set(ids)), f"Duplicate file IDs in tree: {ids}"
    assert any("subdir" in i and "home.py" in i for i in ids), (
        f"Nested file ID must encode the subdir path; got {ids}"
    )
