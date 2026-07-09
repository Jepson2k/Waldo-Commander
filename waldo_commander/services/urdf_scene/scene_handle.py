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
    """Program-layer shape state.

    The GUI never owns the collision world: the backend's applied world is the
    only truth, adopted via :meth:`refresh_from_backend` (on connect, on
    reconnect, and whenever the status stream's ``scene_epoch`` moves). A
    ``shapes`` assignment is a *request* — pushed once with the ABC's
    acknowledged ``set_shapes`` and rendered in draft styling until readback
    confirms it. Nothing is persisted frontend-side.
    """

    def __init__(self) -> None:
        self._groups: dict[str, Any] = {}
        self._shapes: list[Shape] = []
        self._installation: tuple[Shape, ...] = ()
        self._confirmed = False
        self._refresh_seq = 0

    @property
    def shapes(self) -> list[Shape]:
        return self._shapes

    @property
    def installation(self) -> tuple[Shape, ...]:
        """Installation-layer shapes as last reported by the backend."""
        return self._installation

    @property
    def confirmed(self) -> bool:
        """Whether the displayed program layer matches backend readback."""
        return self._confirmed

    @shapes.setter
    def shapes(self, value: list[Shape]) -> None:
        # Local checker first — it validates (invalid input raises with
        # nothing mutated anywhere) and the preview / editing-pose collision
        # queries in this process must see the same world the backend is given.
        shapes = list(value)
        ui_state.active_robot.apply_shapes(shapes)
        self._shapes = shapes
        self._confirmed = False
        self._refresh_seq += 1  # in-flight readbacks predate this edit — discard them
        # Enforcement before cosmetics: the backend push must never be lost to
        # a scene/render problem.
        self._push_shapes()
        self.render()
        self._record_snippet(shapes)

    def render(self) -> None:
        """(Re)draw both layers on the live scene (no-op without one)."""
        us = ui_state.urdf_scene
        if us is None:
            return
        try:
            us.render_shapes(
                self._shapes,
                installation=self._installation,
                draft=not self._confirmed,
            )
        except Exception:
            logger.exception("Keep-out shape render failed (still enforced)")

    def _record_snippet(self, shapes: list[Shape]) -> None:
        """Mirror the edit into the active program as a set_shapes([...]) block
        when recording is on (motion-recorder precedent) — the environment's
        durable home is program code, not GUI state."""
        from waldo_commander.services.motion_recorder import motion_recorder

        try:
            motion_recorder.record_action("set_shapes", shapes=shapes)
        except Exception:
            logger.exception("set_shapes code generation failed")

    def _push_shapes(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Assigned from outside the UI loop (a worker thread, a Selenium
            # driver): the push must still reach the controller, or the
            # display shows a barrier nothing enforces.
            from nicegui import core

            if core.loop is not None and core.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._push_shapes_async(), core.loop)
            return  # no app loop at all — local render only
        loop.create_task(self._push_shapes_async())

    async def _push_shapes_async(self) -> None:
        """One acknowledged push; readback confirms or the draft styling stays.

        No local retry policy: the ABC ack plus the connect/epoch re-query is
        the reliability mechanism (reconciliation, not retries).
        """
        shapes = self._shapes
        err: Exception | None = None
        try:
            code = await waldoctl.commander.client.set_shapes(shapes)
        except NotImplementedError:
            return  # backend without shape support — local render only
        except Exception as e:
            code = -1
            err = e
        if shapes is not self._shapes:
            return  # superseded by a newer assignment
        if code > 0:
            await self.refresh_from_backend()
            return
        logger.error(
            "set_shapes push unconfirmed (code=%s%s) — displayed keep-outs are "
            "NOT enforced by the controller until readback confirms",
            code,
            f": {err}" if err is not None else "",
        )

    async def refresh_from_backend(self) -> None:
        """Adopt the backend's applied world (readback truth) for display and
        this process's preview checker."""
        client = waldoctl.commander.client
        if client is None:
            return
        self._refresh_seq += 1
        seq = self._refresh_seq
        try:
            world = await client.shapes()
        except NotImplementedError:
            return  # backend without shape support
        except Exception as e:
            logger.debug("shapes readback failed: %s", e)
            return
        if world is None:
            return  # unreachable — keep current display, re-query on reconnect
        if seq != self._refresh_seq:
            return  # superseded by a newer edit or readback — that one adopts
        self._installation = tuple(world.installation)
        self._shapes = list(world.program)
        self._confirmed = True
        try:
            ui_state.active_robot.apply_shapes(self._shapes)
        except Exception:
            logger.exception("Local checker sync from readback failed")
        self.render()

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
