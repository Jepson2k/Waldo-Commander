"""Program editor with atomic scene edits, including before its first mount."""

from pathlib import Path
import inspect
from typing import Any

from nicegui.elements.codemirror.codemirror import CodeMirror


class SceneCodeEditor(
    CodeMirror,
    component="scene_code_editor.js",
    esm={
        "wc-native-codemirror": str(Path(inspect.getfile(CodeMirror)).parent),
    },
):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mounted = False
        # The scene's own edits, (before, after), newest last: Ctrl+Z in the
        # scene walks these, whether or not the editor is open, since closing
        # the editor takes its own history with it.
        self._done: list[tuple[str, str]] = []
        self._undone: list[tuple[str, str]] = []
        self.on("scene-mounted", self._on_scene_mounted)
        self.on("scene-unmounted", self._on_scene_unmounted)

    def _on_scene_mounted(self) -> None:
        # Mounting replayed the history; resending it with every later update
        # would carry two copies of the program per edit.
        self._mounted = True
        self._props.pop("scene-history", None)
        self._props.pop("scene-cursor-line", None)

    def _on_scene_unmounted(self) -> None:
        self._mounted = False

    def place_cursor(self, line_number: int) -> None:
        """Reveal a line and place the cursor without taking keyboard focus."""
        if not self._mounted:
            self._props["scene-cursor-line"] = line_number
            self.update()
        else:
            self.run_method("placeCursor", line_number)

    def undo(self) -> None:
        """Take back the scene's last edit, unless the source has changed
        since in some other way."""
        self._step(self._done, self._undone, back=True)

    @property
    def can_undo_scene(self) -> bool:
        return bool(self._done and self._done[-1][1] == str(self.value or ""))

    @property
    def can_redo_scene(self) -> bool:
        return bool(self._undone and self._undone[-1][0] == str(self.value or ""))

    def redo(self) -> None:
        """Make the scene's last undone edit again."""
        self._step(self._undone, self._done, back=False)

    def _step(
        self,
        stack: list[tuple[str, str]],
        other: list[tuple[str, str]],
        *,
        back: bool,
    ) -> None:
        current = str(self.value or "")
        if not stack:
            return
        before, after = stack[-1]
        if current != (after if back else before):
            # Typed over since: the scene's history no longer describes it.
            self._done.clear()
            self._undone.clear()
            return
        other.append(stack.pop())
        self._apply(before if back else after)

    def commit_scene_value(self, value: str) -> None:
        """Set the source as one undoable scene edit."""
        before = str(self.value or "")
        if before == value:
            return
        self._done.append((before, value))
        del self._done[:-50]
        self._undone.clear()
        self._apply(value)

    def _apply(self, value: str) -> None:
        before = str(self.value or "")
        if not self._mounted:
            history = self._props.get("scene-history", [])
            if not history or history[-1][1] != before:
                history = []
            self._props["scene-history"] = [*history[-19:], [before, value]]
        self.value = value
