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
from nicegui import ui

if TYPE_CHECKING:
    from nicegui.testing import User


async def open_file_via_dialog(user: "User", filename: str) -> None:
    """Open a file from server via the tree-based open dialog."""
    user.find(marker="editor-open-btn").click()
    await asyncio.sleep(0)

    # Select the file in the tree by its node id (which is the filename)
    trees = user.find(kind=ui.tree).elements
    for tree in trees:
        tree.props(f'selected="{filename}"')
        tree._event_args["update:selected"]({"args": filename})
        break
    await asyncio.sleep(0)

    user.find(marker="open-confirm-btn").click()
    await asyncio.sleep(0.1)


@pytest.mark.integration
class TestFileOperations:
    """File operation tests using simulated user fixture."""

    async def test_buttons_exist(self, user: "User") -> None:
        """Verify file operation buttons are present when editor is open."""
        await user.open("/")
        user.find(marker="tab-program").click()
        await asyncio.sleep(0)

        await user.should_see(marker="editor-save-btn")
        await user.should_see(marker="editor-open-btn")
        await user.should_see(marker="editor-new-tab-btn")

    async def test_save_dialog_opens(self, user: "User") -> None:
        """Clicking save button opens the save dialog with tree."""
        await user.open("/")
        user.find(marker="tab-program").click()
        await asyncio.sleep(0)

        user.find(marker="editor-save-btn").click()
        await asyncio.sleep(0)

        await user.should_see(marker="save-file-tree")
        await user.should_see(marker="save-confirm-btn")
        await user.should_see(marker="save-download-btn")

    async def test_open_dialog_opens(self, user: "User") -> None:
        """Clicking open button opens the open dialog with tree."""
        await user.open("/")
        user.find(marker="tab-program").click()
        await asyncio.sleep(0)

        user.find(marker="editor-open-btn").click()
        await asyncio.sleep(0)

        await user.should_see(marker="open-file-tree")
        await user.should_see(marker="open-confirm-btn")
        await user.should_see(marker="open-upload")

    async def test_save_to_server_writes_file(self, user: "User") -> None:
        """_save_tab writes file to PROGRAM_DIR."""
        from waldo_commander.state import editor_tabs_state, ui_state

        await user.open("/")
        user.find(marker="tab-program").click()
        await asyncio.sleep(0)

        editor = ui_state.editor_panel
        assert editor is not None

        active_tab = editor_tabs_state.get_active_tab()
        assert active_tab is not None
        test_content = "# Save test\nprint('saved')\n"
        test_filename = "test_save_direct.py"
        active_tab.content = test_content
        active_tab.filename = test_filename

        await editor._save_tab(active_tab)

        test_file = editor.PROGRAM_DIR / test_filename
        try:
            assert test_file.exists(), f"File should exist at {test_file}"
            saved = test_file.read_text(encoding="utf-8")
            assert saved == test_content
        finally:
            if test_file.exists():
                test_file.unlink()

    async def test_new_tab_button(self, user: "User") -> None:
        """Clicking new tab button creates a new tab."""
        from waldo_commander.state import editor_tabs_state

        await user.open("/")
        user.find(marker="tab-program").click()
        await asyncio.sleep(0)

        initial_tab_count = len(editor_tabs_state.tabs)

        user.find(marker="editor-new-tab-btn").click()
        await asyncio.sleep(0)

        assert len(editor_tabs_state.tabs) == initial_tab_count + 1

    async def test_download_triggers(self, user: "User") -> None:
        """Download button in save dialog triggers a download."""
        await user.open("/")
        user.find(marker="tab-program").click()
        await asyncio.sleep(0)

        # Open save dialog
        user.find(marker="editor-save-btn").click()
        await asyncio.sleep(0)

        # Click download
        user.find(marker="save-download-btn").click()

        response = await user.download.next(timeout=2.0)
        assert response.status_code == 200
        assert len(response.content) > 0


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
