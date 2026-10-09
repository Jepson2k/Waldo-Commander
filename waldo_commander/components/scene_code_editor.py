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
        self.on("scene-mounted", self._on_scene_mounted)
        self.on("scene-unmounted", self._on_scene_unmounted)

    def _on_scene_mounted(self) -> None:
        # Mounting replayed the history; resending it with every later update
        # would carry two copies of the program per edit.
        self._mounted = True
        self._props.pop("scene-history", None)

    def _on_scene_unmounted(self) -> None:
        self._mounted = False

    def commit_scene_value(self, value: str) -> None:
        before = str(self.value or "")
        if before == value:
            return
        if not self._mounted:
            history = self._props.get("scene-history", [])
            if not history or history[-1][1] != before:
                history = []
            self._props["scene-history"] = [*history[-19:], [before, value]]
        self.value = value
