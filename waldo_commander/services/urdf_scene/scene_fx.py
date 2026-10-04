"""Client-side scene animations (``static/js/scene-fx.js``).

Python names the objects; the browser animates them on its own frame clock,
so an effect costs one message rather than a stream of transforms. Reveal
effects are queued while a scene update creates objects and flushed as a
single call once that update's batch has gone out.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from nicegui import ui


def _run(scene: ui.scene, call: str) -> None:
    """Run a ``SceneFx`` call once the deferred script has loaded."""
    if scene.is_deleted:
        return
    scene.client.run_javascript(
        "(function go(n){if(window.SceneFx){" + call + "}"
        "else if(n>0){setTimeout(function(){go(n-1)},50)}})(60);"
    )


class SceneFx:
    """Animations for one :class:`ui.scene`."""

    def __init__(self) -> None:
        self._segments: list[list[str]] = []
        self._markers: list[str] = []

    def queue_segment(self, objects: Iterable[Any]) -> None:
        """Draw in a new path segment: its polyline first, then its cones."""
        ids = [obj.id for obj in objects]
        if ids:
            self._segments.append(ids)

    def queue_marker(self, obj: Any) -> None:
        """Pop in a new object: a waypoint or target marker, a keep-out shape."""
        self._markers.append(obj.id)

    def flush(self, scene: ui.scene | None) -> None:
        """Send the queued reveals; call after the creating batch has flushed."""
        if not self._segments and not self._markers:
            return
        segments, markers = self._segments, self._markers
        self._segments, self._markers = [], []
        if scene is None:
            return
        _run(
            scene,
            f"SceneFx.reveal({scene.id}, {json.dumps(segments)}, {json.dumps(markers)})",
        )

    @staticmethod
    def flash(scene: ui.scene, objects: Iterable[Any]) -> None:
        """Glow-and-pop objects that just appeared (a newly mounted tool)."""
        ids = [obj.id for obj in objects]
        if ids:
            _run(scene, f"SceneFx.flash({scene.id}, {json.dumps(ids)})")

    @staticmethod
    def alarm(scene: ui.scene, objects: Iterable[Any], color: str) -> None:
        """Flash objects that just started colliding in *color*."""
        ids = [obj.id for obj in objects]
        if ids:
            _run(
                scene,
                f"SceneFx.alarm({scene.id}, {json.dumps(ids)}, {json.dumps(color)})",
            )

    @staticmethod
    def fade_in(scene: ui.scene, objects: Iterable[Any]) -> None:
        """Fade objects that were just shown up to their own opacity."""
        ids = [obj.id for obj in objects]
        if ids:
            _run(scene, f"SceneFx.fadeIn({scene.id}, {json.dumps(ids)})")

    @staticmethod
    def spring_back(
        scene: ui.scene,
        obj: Any,
        start: tuple[float, float, float],
        miss_color: str | None = None,
    ) -> None:
        """Spring *obj* back from *start* (in its parent's frame) to the
        position just set on it, tinted *miss_color* when the drag asked
        for an unreachable pose."""
        _run(
            scene,
            f"SceneFx.springBack({scene.id}, {json.dumps(obj.id)}, "
            f"{json.dumps(list(start))}, {json.dumps(miss_color)})",
        )

    @staticmethod
    def pulse(scene: ui.scene, objects: Iterable[Any]) -> None:
        """Ripple *objects* in order for a few waves; the next call replaces
        them, and an empty one stops it."""
        ids = [obj.id for obj in objects]
        _run(scene, f"SceneFx.pulse({scene.id}, {json.dumps(ids)})")

    @staticmethod
    def ease_camera(scene: ui.scene) -> None:
        """Ease the ``move_camera`` tween that was just started."""
        _run(scene, f"SceneFx.easeCamera({scene.id})")
