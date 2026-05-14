"""File operations mixin for the editor: save, open, upload, download dialogs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nicegui import ui

from waldo_commander.state import EditorTab, editor_tabs_state

logger = logging.getLogger(__name__)


class FileOperationsMixin:
    """Mixin providing file save/open/upload/download for EditorPanel."""

    # Provided by EditorPanel (accessed via self)
    PROGRAM_DIR: Path
    _tab_widgets: dict[str, dict[str, Any]]
    _new_tab: Any
    _switch_to_tab: Any
    _do_close_tab: Any

    def _update_dirty_dot(self, tab: EditorTab) -> None:
        widgets = self._tab_widgets.get(tab.id, {})
        dirty_dot = widgets.get("dirty_dot")
        if dirty_dot:
            dirty_dot.set_visibility(tab.is_dirty)

    # ---- Core file I/O ----

    async def load_program(self, filename: str | None = None) -> None:
        """Load a program file into a new tab (or switch if already open)."""
        try:
            name = filename or ""
            if not name:
                tab = editor_tabs_state.get_active_tab()
                if tab:
                    name = tab.filename
            if not name:
                ui.notify("No filename specified", color="warning")
                return

            file_path = str(self.PROGRAM_DIR / name)

            existing_tab = editor_tabs_state.find_tab_by_path(file_path)
            if existing_tab:
                self._switch_to_tab(existing_tab.id)
                return

            text = (self.PROGRAM_DIR / name).read_text(encoding="utf-8")
            tab = self._new_tab(filename=name, content=text)
            tab.file_path = file_path
            tab.saved_content = text
            self._update_dirty_dot(tab)
            logger.info("Loaded program %s", name)
        except Exception as e:
            ui.notify(f"Load failed: {e}", color="negative")
            logger.error("Load failed: %s", e)

    async def save_program(self) -> None:
        """Save the active tab's program to a file."""
        tab = editor_tabs_state.get_active_tab()
        if not tab:
            ui.notify("No active tab to save", color="warning")
            return
        await self._save_tab(tab)

    def download_program(self) -> None:
        """Download the active tab's program content to the user's device."""
        tab = editor_tabs_state.get_active_tab()
        if not tab:
            ui.notify("No active tab to download", color="warning")
            return
        self._download_tab(tab)

    async def _save_tab(self, tab: EditorTab) -> None:
        """Save tab content to server."""
        try:
            name = tab.filename or "program.py"
            file_path = str(self.PROGRAM_DIR / name)
            (self.PROGRAM_DIR / name).write_text(tab.content, encoding="utf-8")
            tab.file_path = file_path
            tab.saved_content = tab.content
            self._update_dirty_dot(tab)
            logger.info("Saved program %s", name)
        except Exception as e:
            ui.notify(f"Save failed: {e}", color="negative")
            logger.error("Save failed: %s", e)

    async def _save_tab_and_close(self, tab: EditorTab, dlg: ui.dialog) -> None:
        """Save tab and close it."""
        await self._save_tab(tab)
        dlg.close()
        self._do_close_tab(tab)

    def _download_tab(self, tab: EditorTab) -> None:
        """Download tab content to user's device."""
        content = tab.content
        if not content:
            ui.notify("No content to download", color="warning")
            return
        filename = tab.filename.strip() or "program.py"
        ui.download(content.encode("utf-8"), filename)
        logger.info("Downloaded program %s", filename)

    # ---- File tree helpers ----

    @staticmethod
    def _build_file_tree(root: Path, base: Path) -> list[dict]:
        """Build child node list for ui.tree from a directory (recursive)."""
        nodes: list[dict] = []
        try:
            for item in sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name)):
                if item.is_dir() and not item.name.startswith("."):
                    children = FileOperationsMixin._build_file_tree(item, base)
                    if children:
                        nodes.append(
                            {
                                "id": str(item),
                                "label": item.name,
                                "icon": "folder",
                                "children": children,
                            }
                        )
                elif item.is_file() and item.suffix in (".py", ".txt", ".prog", ""):
                    nodes.append(
                        {
                            "id": str(item.relative_to(base)),
                            "label": item.name,
                            "icon": "description",
                        }
                    )
        except OSError:
            pass
        return nodes

    def _file_tree_nodes(self) -> list[dict]:
        """Build root tree nodes for the programs directory."""
        return [
            {
                "id": str(self.PROGRAM_DIR),
                "label": self.PROGRAM_DIR.name,
                "icon": "folder",
                "children": self._build_file_tree(self.PROGRAM_DIR, self.PROGRAM_DIR),
            }
        ]

    def _create_file_tree(self, marker: str) -> ui.tree:
        """Create a file tree widget with the programs directory expanded."""
        tree = ui.tree(
            self._file_tree_nodes(),
            node_key="id",
            label_key="label",
        ).props("dense text-color=grey selected-color=white")
        tree.classes("file-tree")
        tree.expand()
        tree.mark(marker)
        return tree

    # ---- Dialogs ----

    def _show_save_dialog(self) -> None:
        """Show save dialog with file tree and download option."""
        tab = editor_tabs_state.get_active_tab()
        if not tab:
            return

        dlg = ui.dialog()
        with dlg, ui.card().classes("overlay-card gap-0").style("width: 400px;"):
            with ui.row().classes("w-full items-center"):
                ui.label("Save").classes("text-lg font-medium")
                ui.space()
                ui.button(icon="close", on_click=dlg.close).props(
                    "flat round dense color=white"
                )

            filename_input = (
                ui.input("Filename", value=tab.filename or "program.py")
                .props("dense")
                .classes("w-full")
            )

            with (
                ui.scroll_area()
                .style("max-height: 250px;")
                .classes("w-full file-tree-scroll")
            ):
                tree = self._create_file_tree("save-file-tree")

                def on_select(e):
                    val = e.value
                    if val and not Path(val).is_dir():
                        filename_input.value = val

                tree.on_select(on_select)

            async def do_save():
                name = filename_input.value.strip()
                if name:
                    tab.filename = name
                    await self._save_tab(tab)
                dlg.close()

            def do_download():
                self.download_program()
                dlg.close()

            with ui.row().classes("w-full items-center mt-2"):
                ui.button("Download", icon="download", on_click=do_download).props(
                    "flat color=white"
                ).mark("save-download-btn")
                ui.space()
                ui.button("Save", on_click=do_save).props("color=primary").mark(
                    "save-confirm-btn"
                )

        dlg.open()

    def _show_open_dialog(self) -> None:
        """Show open dialog with file tree and inline upload."""
        selected_file: list[str | None] = [None]

        dlg = ui.dialog()
        with dlg, ui.card().classes("overlay-card gap-0").style("width: 400px;"):
            with ui.row().classes("w-full items-center"):
                ui.label("Open").classes("text-lg font-medium")
                ui.space()
                ui.button(icon="close", on_click=dlg.close).props(
                    "flat round dense color=white"
                )

            with (
                ui.scroll_area()
                .style("max-height: 250px;")
                .classes("w-full file-tree-scroll")
            ):
                tree = self._create_file_tree("open-file-tree")

                def on_select(e):
                    val = e.value
                    if val and not Path(val).is_dir():
                        selected_file[0] = val

                tree.on_select(on_select)

            async def _on_upload(e):
                try:
                    data = await e.file.read()
                    name = e.file.name or "uploaded_program.txt"
                    content = data.decode("utf-8", errors="ignore")

                    file_path = str(self.PROGRAM_DIR / name)
                    (self.PROGRAM_DIR / name).write_bytes(data)

                    existing_tab = editor_tabs_state.find_tab_by_path(file_path)
                    if existing_tab:
                        existing_tab.content = content
                        existing_tab.saved_content = content
                        widgets = self._tab_widgets.get(existing_tab.id, {})
                        textarea = widgets.get("textarea")
                        if textarea:
                            textarea.value = content
                        self._switch_to_tab(existing_tab.id)
                    else:
                        tab = self._new_tab(filename=name, content=content)
                        tab.file_path = file_path
                        tab.saved_content = content

                    dlg.close()
                except Exception as ex:
                    ui.notify(f"Upload failed: {ex}", color="negative")
                    logger.error("File upload failed: %s", ex)

            ui.upload(
                on_upload=_on_upload,
                label="Drop .py file here or click to browse",
            ).props('accept=".py" max-file-size=10485760 flat color=teal').classes(
                "w-full file-upload"
            ).mark("open-upload")

            async def do_open():
                fname = selected_file[0]
                if fname:
                    await self.load_program(fname)
                    dlg.close()

            with ui.row().classes("w-full justify-end mt-2"):
                ui.button("Open", on_click=do_open).props("color=primary").mark(
                    "open-confirm-btn"
                )

        dlg.open()
