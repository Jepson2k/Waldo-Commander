"""Coalesce ``scene.run_method`` calls into a single WS frame.

NiceGUI's ``Object3D.move/rotate/scale/material/...`` each emit a separate
``run_method`` → ``run_javascript`` → outbox message → WS frame. When many
of these fire in one Python tick (e.g. 6 joints × move+rotate per call to
``UrdfScene.set_axis_values``), three.js can render frames with only a
subset applied, producing visible "shake" while the wrist sits in a
partially-updated pose.

The ``batch_scene`` context manager temporarily redirects ``scene.run_method``
to a queue, then on exit flushes the queue as one ``run_javascript`` that
loops through ``runMethod(scene_id, name, args)`` synchronously. The browser
applies the full batch before its next render — atomic visual update.

Inside the block, all Object3D state updates (``self.x``, ``self.R``, etc.)
still happen normally because they're set *before* ``_move()``/``_rotate()``
calls into the patched ``run_method``. We don't touch Object3D internals.

Constraints:
    * Batched calls are fire-and-forget. Awaiting one inside the block
      raises — see ``BatchedAwaitError``.
    * Nested ``batch_scene`` blocks are flat: the outermost block is the
      one that flushes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from nicegui.awaitable_response import AwaitableResponse


class BatchedAwaitError(RuntimeError):
    """Raised when code tries to ``await`` a scene.run_method inside batch_scene."""


class _BatchedNullResponse(AwaitableResponse):
    """Fire-and-forget response that loudly rejects awaits.

    Mirrors ``nicegui.awaitable_response.NullResponse``'s pattern of
    skipping ``super().__init__()`` (which would schedule a background
    task we don't want); see nicegui's class for precedent.
    """

    def __init__(self) -> None:
        self._is_fired = True
        self._is_awaited = False

    def __await__(self):
        raise BatchedAwaitError(
            "Cannot await a scene.run_method call inside `with batch_scene(...):`. "
            "Batched calls are fire-and-forget. Move calls that need a return value "
            "outside the batch."
        )


# Module-level singleton — `_BatchedNullResponse` has no per-instance
# mutable state, so every `_enqueue` call can return the same object
# instead of allocating a fresh one at ~600/sec hot-path rates.
_BATCHED_NULL = _BatchedNullResponse()


@contextmanager
def batch_scene(scene: Any) -> Iterator[None]:
    """Send all ``scene.run_method`` calls inside this block as one WS frame.

    Args:
        scene: A ``nicegui.ui.scene`` instance whose ``run_method`` should
            be temporarily redirected to a queue.

    Yields:
        Nothing — the block body runs unmodified; the flush happens on exit.

    Example:
        >>> with batch_scene(self.scene):
        ...     for name, q in zip(joint_names, values):
        ...         t, r = trafos[name](q)
        ...         joint_groups[name].move(*t).rotate(*r)
        # On exit: all 12 transforms applied atomically before three.js
        # renders its next frame.

    Note:
        Body must stay synchronous. Awaiting inside the block lets
        another task observe the patched ``scene.run_method`` and the
        marker attribute, which can corrupt state when the inner task
        also enters ``batch_scene`` on the same scene.
    """
    # Check if we're already batching on this scene (nested batch_scene).
    # If so, the outer one owns the queue; we no-op.
    if getattr(scene, "_wc_batched_calls", None) is not None:
        yield
        return

    queued: list[tuple[str, tuple]] = []
    scene._wc_batched_calls = queued
    original = scene.run_method

    def _enqueue(name: str, *args: Any, timeout: float = 1) -> AwaitableResponse:
        queued.append((name, args))
        return _BATCHED_NULL

    scene.run_method = _enqueue
    try:
        yield
    finally:
        scene.run_method = original
        scene._wc_batched_calls = None
        if not queued:
            return
        # One json.dumps of the whole queue (C-implemented), one f-string
        # assembly, one run_javascript. The browser parses and executes the
        # IIFE synchronously before its next render frame.
        #
        # Use ``scene.client.run_javascript`` rather than ``ui.run_javascript``
        # so the flush works from any task / outside the request slot context
        # (e.g. status_consumer, background timers). ``ui.run_javascript``
        # relies on the current slot stack and would raise
        # "The current slot cannot be determined" otherwise.
        scene.client.run_javascript(
            f"(function(){{const b={json.dumps(queued)};"
            f"for(const[n,a]of b)runMethod({json.dumps(scene.id)},n,a);}})();"
        )
