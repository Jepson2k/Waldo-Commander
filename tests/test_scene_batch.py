"""Unit tests for ``waldo_commander.services.urdf_scene.scene_batch``.

These are pure sync tests — ``batch_scene`` itself doesn't await, so we
don't need pytest-asyncio. Mocks follow the ``MagicMock`` pattern in
``tests/test_scene_diff_rendering.py::TestCollectFailedTarget``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from waldo_commander.services.urdf_scene.scene_batch import (
    _BATCHED_NULL,
    BatchedAwaitError,
    batch_scene,
)


def _make_scene() -> MagicMock:
    """Mock a nicegui scene with the attributes batch_scene touches."""
    scene = MagicMock()
    scene.id = "scene-id-42"
    # Without this, MagicMock() auto-creates _wc_batched_calls as a Mock,
    # which getattr(..., None) would not return None for.
    scene._wc_batched_calls = None
    return scene


def test_basic_flush() -> None:
    """A batch of N calls produces exactly one client.run_javascript with
    a JS payload containing all N method names in queued order."""
    scene = _make_scene()
    with batch_scene(scene):
        scene.run_method("move", 1.0, 2.0, 3.0)
        scene.run_method("rotate", 0.1, 0.2, 0.3)
        scene.run_method("material", "#ff0000", 0.5)

    assert scene.client.run_javascript.call_count == 1
    payload = scene.client.run_javascript.call_args.args[0]
    # The JS payload embeds json.dumps of the queue. Substring check is
    # adequate; the IIFE wrapper is asserted indirectly via the structure.
    assert '"move"' in payload
    assert '"rotate"' in payload
    assert '"material"' in payload
    assert (
        payload.index('"move"')
        < payload.index('"rotate"')
        < payload.index('"material"')
    )
    assert json.dumps(scene.id) in payload  # scene_id correctly embedded


def test_restores_run_method_on_exit() -> None:
    """After a clean batch, scene.run_method is the original object again."""
    scene = _make_scene()
    original = scene.run_method
    with batch_scene(scene):
        scene.run_method("move", 0.0, 0.0, 0.0)
    assert scene.run_method is original
    assert scene._wc_batched_calls is None


def test_restores_run_method_on_exception() -> None:
    """Exception inside the block still restores run_method and flushes
    whatever was queued before the raise."""
    scene = _make_scene()
    original = scene.run_method

    with pytest.raises(RuntimeError, match="body kaboom"):
        with batch_scene(scene):
            scene.run_method("move", 1.0, 2.0, 3.0)
            raise RuntimeError("body kaboom")

    # run_method restored
    assert scene.run_method is original
    assert scene._wc_batched_calls is None
    # Partial flush happened
    assert scene.client.run_javascript.call_count == 1
    assert '"move"' in scene.client.run_javascript.call_args.args[0]


def test_nested_no_op() -> None:
    """The inner batch_scene block doesn't re-patch run_method; the
    outer block's queue collects everything from both."""
    scene = _make_scene()
    outer_original = scene.run_method

    with batch_scene(scene):
        patched_at_outer = scene.run_method
        scene.run_method("outer_call", 1.0)
        with batch_scene(scene):
            # Inner is a no-op: run_method should still be the outer's patch
            assert scene.run_method is patched_at_outer
            scene.run_method("inner_call", 2.0)
        # After the inner exit, outer's patch must still be in place
        assert scene.run_method is patched_at_outer

    assert scene.run_method is outer_original
    # Exactly one flush, containing both calls
    assert scene.client.run_javascript.call_count == 1
    payload = scene.client.run_javascript.call_args.args[0]
    assert '"outer_call"' in payload
    assert '"inner_call"' in payload


def test_empty_batch_no_flush() -> None:
    """An empty batch doesn't issue a run_javascript call (no point)."""
    scene = _make_scene()
    with batch_scene(scene):
        pass
    assert scene.client.run_javascript.call_count == 0


def test_batched_await_raises() -> None:
    """Iterating the singleton's __await__ generator surfaces
    BatchedAwaitError immediately — catches accidental awaits inside
    the batch body."""
    with pytest.raises(BatchedAwaitError, match="fire-and-forget"):
        # __await__ is what `await` desugars to; iterate it directly so
        # we don't need an event loop.
        next(_BATCHED_NULL.__await__())
