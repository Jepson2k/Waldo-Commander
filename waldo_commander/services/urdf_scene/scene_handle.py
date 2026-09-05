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
        self._installation_draft: tuple[Shape, ...] = ()
        self._confirmed = False
        self._refresh_seq = 0
        self._pushes_inflight = 0

    @property
    def shapes(self) -> list[Shape]:
        return self._shapes

    @property
    def installation_draft(self) -> tuple[Shape, ...]:
        return self._installation_draft

    def propose_installation(self, names: list[str]) -> None:
        """Move program shapes into the installation proposal: they leave the
        layer the backend enforces, and this process keeps checking them so
        the design can be flown against before the config declares it."""
        wanted = set(names)
        moving = [s for s in self._shapes if s.name in wanted]
        missing = sorted(wanted - {s.name for s in moving})
        if missing:
            raise ValueError(f"no program-layer shape(s) named {missing}")
        self._assign(
            [s for s in self._shapes if s.name not in wanted],
            (*self._installation_draft, *moving),
            push=True,
        )

    def withdraw_proposal(self, name: str) -> None:
        """Return a proposed shape to the program layer, in one move.

        Both layers change together so a refusal leaves neither touched —
        discarding first and re-adding after would destroy the drafted
        geometry whenever the re-add is rejected, and the caller would
        have nothing to put back.
        """
        shape = next((s for s in self._installation_draft if s.name == name), None)
        if shape is None:
            raise ValueError(f"no proposed shape named {name!r}")
        self._assign(
            [*self._shapes, shape],
            tuple(s for s in self._installation_draft if s.name != name),
            push=True,
        )

    def discard_installation_draft(self, names: list[str] | None = None) -> None:
        drop = None if names is None else set(names)
        self._assign(
            self._shapes,
            ()
            if drop is None
            else tuple(s for s in self._installation_draft if s.name not in drop),
            push=False,
        )

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
        self._assign(list(value), self._installation_draft, push=True)

    @property
    def enforced_locally(self) -> list[Shape]:
        """What this process's checker holds: the program layer plus the
        installation proposal, which nothing else enforces yet."""
        return [*self._shapes, *self._installation_draft]

    def _assign(
        self, shapes: list[Shape], draft: tuple[Shape, ...], *, push: bool
    ) -> None:
        """Move both layers at once, validating before either changes.

        The local checker takes program plus proposal in one call: a
        proposal is not enforced anywhere until the robot config declares
        it, so enforcing it here is what lets you design against it. The
        backend is sent the program layer alone. ``push`` is False when only
        the proposal moved — the enforced world did not change, so there is
        nothing to send and nothing to re-confirm.
        """
        drafted = {d.name for d in draft}
        clash = sorted(s.name for s in shapes if s.name in drafted)
        if clash:
            raise ValueError(
                f"shape name(s) {clash} are proposed for the installation layer; "
                "discard the proposal or pick another name"
            )
        # Validates first: invalid input raises with nothing mutated anywhere.
        ui_state.active_robot.apply_shapes([*shapes, *draft])
        self._shapes = shapes
        self._installation_draft = draft
        if push:
            self._confirmed = False
            self._refresh_seq += 1  # in-flight readbacks predate this edit
            # Enforcement before cosmetics: the backend push must never be
            # lost to a scene/render problem.
            self._push_shapes()
        self.render()
        if push:
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
                installation_draft=self._installation_draft,
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
        # The pushed world is bound HERE and the in-flight count bumped HERE,
        # synchronously: a refresh task created before this edit must see the
        # gate closed when it runs, and the push coroutine must not re-read
        # self._shapes at start (a raced adopt may have changed it by then).
        shapes = self._shapes
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Assigned from outside the UI loop (a worker thread, a Selenium
            # driver): the push must still reach the controller, or the
            # display shows a barrier nothing enforces.
            from nicegui import core

            if core.loop is not None and core.loop.is_running():
                self._pushes_inflight += 1
                asyncio.run_coroutine_threadsafe(
                    self._push_shapes_async(shapes), core.loop
                )
            return  # no app loop at all — local render only
        self._pushes_inflight += 1
        loop.create_task(self._push_shapes_async(shapes))

    async def _push_shapes_async(self, shapes: list[Shape]) -> None:
        """One acknowledged push; readback confirms or the draft styling stays.

        No local retry policy: the ABC ack plus the connect/epoch re-query is
        the reliability mechanism (reconciliation, not retries).
        """
        err: Exception | None = None
        try:
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
                await self._adopt_backend_world()
                return
            logger.error(
                "set_shapes push unconfirmed (code=%s%s) — displayed keep-outs are "
                "NOT enforced by the controller until readback confirms",
                code,
                f": {err}" if err is not None else "",
            )
        finally:
            self._pushes_inflight -= 1

    async def refresh_from_backend(self) -> None:
        """Adopt the backend's applied world (readback truth) for display and
        this process's preview checker.

        No-op while a shapes push is awaiting its ack: a readback started in
        that window queries the pre-edit world and would resurrect it (and the
        push's supersession check then skips its own corrective refresh). The
        push adopts the applied world itself once acked."""
        if self._pushes_inflight:
            return
        await self._adopt_backend_world()

    async def _adopt_backend_world(self) -> None:
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
        # A proposal the backend now enforces is no longer a proposal. By
        # name: the config is authored by hand from the exported TOML, so the
        # enforced shape rarely compares equal to the drafted one field for
        # field, and a name is what both layers are keyed by anyway.
        #
        # The PROGRAM layer counts too. Readback is adopted wholesale — it
        # is truth — so a program shape that arrives under a drafted name
        # would otherwise leave both layers holding it, and `_assign`'s
        # clash check would then refuse every subsequent edit, including
        # from handlers that do not catch it. The proposal loses; the
        # backend is enforcing that name now, and the draft is not.
        adopted = {s.name for s in self._installation}
        adopted |= {s.name for s in self._shapes}
        self._installation_draft = tuple(
            s for s in self._installation_draft if s.name not in adopted
        )
        try:
            ui_state.active_robot.apply_shapes(self.enforced_locally)
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
