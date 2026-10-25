"""``commander.scene`` implementation over the core ``UrdfScene``.

Lets plugins draw into named, plugin-owned groups of the shared 3D scene. The
scene is created per page (and may not exist yet), so the handle resolves
``ui_state.urdf_scene`` lazily on each call and no-ops when there is no live
scene. Each ``overlay`` deletes the group's prior contents and draws anew; what
one pass of the event loop draws reaches the browser as one update.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import waldoctl
from waldoctl import Shape

from nicegui import core

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
        self._attachment_epoch = 0
        self._refresh_seq = 0
        self._pushes_inflight = 0
        self._push_lock = asyncio.Lock()
        self.revision = 0
        self.last_error: str | None = None
        # The revision whose push ended with neither an ack nor a readback.
        self._failed_revision: int | None = None
        self._world_changed = asyncio.Event()

    def _signal_world_change(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if core.loop is not None and core.loop.is_running() and loop is not core.loop:
            core.loop.call_soon_threadsafe(self._signal_world_change)
            return
        self._world_changed.set()
        self._world_changed = asyncio.Event()

    async def wait_for_confirmation(self, revision: int) -> None:
        while (
            not self._confirmed
            and self.revision == revision
            and self._failed_revision != revision
        ):
            await self._world_changed.wait()
        if self._failed_revision == revision and not self._confirmed:
            raise ValueError(
                self.last_error or "The controller did not answer the object edit"
            )
        if not self._confirmed:
            raise ValueError("Object edit superseded by another world change")

    @property
    def shapes(self) -> list[Shape]:
        return self._shapes

    @property
    def installation_draft(self) -> tuple[Shape, ...]:
        return self._installation_draft

    def proposal_layers(
        self, names: list[str]
    ) -> tuple[list[Shape], tuple[Shape, ...]]:
        """The program layer and proposal after moving *names* into the proposal."""
        wanted = set(names)
        moving = [s for s in self._shapes if s.name in wanted]
        missing = sorted(wanted - {s.name for s in moving})
        if missing:
            raise ValueError(f"no program-layer shape(s) named {missing}")
        if any(s.attachment is not None for s in moving):
            raise ValueError(
                "detach held geometry before proposing installation shapes"
            )
        return (
            [s for s in self._shapes if s.name not in wanted],
            (*self._installation_draft, *moving),
        )

    def withdrawal_layers(self, name: str) -> tuple[list[Shape], tuple[Shape, ...]]:
        """The program layer and proposal after *name* returns to the program.

        Both layers change together so a refusal leaves neither touched —
        discarding first and re-adding after would destroy the drafted
        geometry whenever the re-add is rejected, and the caller would
        have nothing to put back.
        """
        shape = next((s for s in self._installation_draft if s.name == name), None)
        if shape is None:
            raise ValueError(f"no proposed shape named {name!r}")
        return (
            [*self._shapes, shape],
            tuple(s for s in self._installation_draft if s.name != name),
        )

    def propose_installation(self, names: list[str]) -> None:
        """Move program shapes into the installation proposal: they leave the
        layer the backend enforces, and this process keeps checking them so
        the design can be flown against before the config declares it."""
        self._assign(*self.proposal_layers(names), push=True)

    def withdraw_proposal(self, name: str) -> None:
        """Return a proposed shape to the program layer, in one move."""
        self._assign(*self.withdrawal_layers(name), push=True)

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

    @property
    def attachments_valid(self) -> bool:
        return all(
            s.attachment is None or s.attachment.epoch == self._attachment_epoch
            for s in self._shapes
        )

    @property
    def attachment_epoch(self) -> int:
        return self._attachment_epoch

    @shapes.setter
    def shapes(self, value: list[Shape]) -> None:
        self._assign(list(value), self._installation_draft, push=True)

    @property
    def enforced_locally(self) -> list[Shape]:
        """What this process's checker holds: the program layer plus the
        installation proposal, which nothing else enforces yet."""
        return [*self._shapes, *self._installation_draft]

    def _assign(
        self,
        shapes: list[Shape],
        draft: tuple[Shape, ...],
        *,
        push: bool,
        record: bool = True,
    ) -> asyncio.Task | None:
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
        if shapes != self._shapes or draft != self._installation_draft:
            self.revision += 1
        self._shapes = shapes
        self._installation_draft = draft
        task = None
        if push:
            self._confirmed = False
            self.last_error = None
            self._refresh_seq += 1  # in-flight readbacks predate this edit
            # Enforcement before cosmetics: the backend push must never be
            # lost to a scene/render problem.
            task = self._push_shapes()
        self.render()
        if push and record:
            self._record_snippet(shapes)
        self._signal_world_change()
        return task

    def render(self) -> None:
        """(Re)draw both layers on the live scene (no-op without one).

        Assigned from outside the UI loop (a worker thread), the drawing is
        handed to the loop whole, since only the loop may change the scene.
        """
        us = ui_state.urdf_scene
        if us is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if core.loop is not None and core.loop.is_running():
                core.loop.call_soon_threadsafe(self.render)
            return
        try:
            us.render_shapes(
                self._shapes,
                installation=self._installation,
                draft=not self._confirmed,
                installation_draft=self._installation_draft,
                attachment_epoch=self._attachment_epoch,
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

    def _push_shapes(self) -> asyncio.Task | None:
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
        return loop.create_task(self._push_shapes_async(shapes))

    async def _push_shapes_async(self, shapes: list[Shape]) -> None:
        """One acknowledged push; readback confirms or the draft styling stays.

        No local retry policy: the ABC ack plus the connect/epoch re-query is
        the reliability mechanism (reconciliation, not retries).
        """
        err: Exception | None = None
        try:
            async with self._push_lock:
                if shapes is not self._shapes:
                    return
                try:
                    code = await waldoctl.commander.client.set_shapes(shapes)
                except NotImplementedError:
                    self.last_error = "This backend does not acknowledge object edits"
                    code = None
                except Exception as e:
                    code = -1
                    err = e
                if shapes is not self._shapes:
                    return  # superseded by a newer assignment
                if code is not None and code <= 0:
                    self.last_error = (
                        str(err)
                        if err is not None
                        else "Object edit was not acknowledged (code 0)"
                        if code == 0
                        else f"Object edit refused (code {code})"
                    )
                    logger.warning("set_shapes not confirmed: %s", self.last_error)
                await self._adopt_backend_world()
                if code is None or code <= 0:
                    self.render()
                if not self._confirmed and shapes is self._shapes:
                    # Nothing read back either: whoever waits on this edit
                    # hears it failed rather than waiting for a readback.
                    self.last_error = (
                        self.last_error
                        or "The controller did not answer the object edit"
                    )
                    self._failed_revision = self.revision
                    self._signal_world_change()
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
        if self._shapes != list(world.program) or self._installation != tuple(
            world.installation
        ):
            self.revision += 1
        self._installation = tuple(world.installation)
        self._attachment_epoch = world.attachment_epoch
        self._shapes = list(world.program)
        self._confirmed = True
        self._signal_world_change()
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
        """Draw into the plugin group *group_id*, replacing what it held.

        Yields the 3D scene, whose ``group``, ``line``, ``polyline``,
        ``box``, ``sphere``, ``cylinder``, ``capsule`` and ``stl`` draw into
        the group; each returns an object with ``move``, ``rotate_R``,
        ``scale``, ``material`` and ``visible``.
        """
        scene = self._live_scene()
        if scene is None:
            yield _NULL_SCENE
            return
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
        self._drop(group_id)
