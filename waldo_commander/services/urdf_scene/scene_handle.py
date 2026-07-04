"""``commander.scene`` implementation over the core ``UrdfScene``.

Lets plugins draw into named, plugin-owned groups of the shared 3D scene. The
scene is created per page (and may not exist yet), so the handle resolves
``ui_state.urdf_scene`` lazily on each call and no-ops when there is no live
scene. Each ``overlay`` deletes the group's prior contents and re-adds inside a
``batch_scene`` so updates apply atomically.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import waldoctl
from waldoctl import Shape

from waldo_commander.services.urdf_scene.scene_batch import batch_scene
from waldo_commander.state import ui_state

logger = logging.getLogger(__name__)


class _NullScene:
    """No-op stand-in so plugin draw calls are safe when no scene is live.

    Supports the context-manager protocol so ``with null_scene.group():`` (the
    usual NiceGUI scene-drawing idiom) is also a no-op — ``__getattr__`` alone
    wouldn't, since ``with`` looks ``__enter__`` / ``__exit__`` up on the type.
    """

    def __getattr__(self, _name: str):
        return lambda *a, **k: self

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None


_NULL_SCENE = _NullScene()


class WcSceneHandle:
    def __init__(self) -> None:
        self._groups: dict[str, Any] = {}
        self._shapes: list[Shape] = []

    @property
    def shapes(self) -> list[Shape]:
        return self._shapes

    @shapes.setter
    def shapes(self, value: list[Shape]) -> None:
        # Local checker first — it validates (e.g. duplicate names raise with
        # nothing mutated anywhere) and the preview / editing-pose collision
        # queries in this process must see the same world the backend is given.
        shapes = list(value)
        ui_state.active_robot.apply_shapes(shapes)
        self._shapes = shapes
        us = ui_state.urdf_scene
        if us is not None:
            us.render_shapes(self._shapes)
        self._push_shapes()

    def _push_shapes(self) -> None:
        """Push the active shapes to the backend's checkers (retried)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop yet — local render only
        loop.create_task(self._push_shapes_async())

    async def _push_shapes_async(self) -> None:
        shapes = self._shapes
        for attempt in range(3):
            try:
                await waldoctl.commander.client.set_shapes(shapes)
                return
            except NotImplementedError:
                return  # backend without shape support — local render only
            except Exception as e:
                if shapes is not self._shapes:
                    return  # superseded by a newer assignment — let its push win
                if attempt == 2:
                    logger.error(
                        "set_shapes push failed — displayed keep-outs are NOT "
                        "enforced by the controller: %s",
                        e,
                    )
                    return
                await asyncio.sleep(0.5 * (attempt + 1))

    def _live_scene(self) -> Any | None:
        us = ui_state.urdf_scene
        scene = us.scene if us is not None else None
        if scene is None or scene.is_deleted:
            return None
        return scene

    def _drop(self, group_id: str) -> None:
        old = self._groups.pop(group_id, None)
        if old is not None:
            try:
                old.delete()
            except Exception as e:
                logger.debug("stale overlay group %r delete: %s", group_id, e)

    @contextmanager
    def overlay(self, group_id: str) -> Iterator[Any]:
        scene = self._live_scene()
        if scene is None:
            yield _NULL_SCENE
            return
        with batch_scene(scene):
            with scene:
                self._drop(group_id)
                grp = scene.group().with_name(f"plugin:{group_id}")
                self._groups[group_id] = grp
                with grp:
                    yield scene

    def clear(self, group_id: str) -> None:
        scene = self._live_scene()
        if scene is None:
            self._groups.pop(group_id, None)
            return
        with batch_scene(scene):
            self._drop(group_id)
