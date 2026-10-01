"""Unit tests for ``waldo_commander.services.urdf_scene.scene_batch``.

These are pure sync tests — ``batch_scene`` itself doesn't await, so we
don't need pytest-asyncio. The scene is a ``MagicMock`` standing in for the
attributes ``batch_scene`` touches; the requirement under test is how many
``run_javascript`` calls reach the client and what they carry.
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


def test_a_batch_flushes_once_in_order_and_restores_the_scene() -> None:
    """N calls in a batch reach the client as one run_javascript carrying
    all N in queued order; a nested batch joins the outer one, an empty one
    sends nothing, and run_method is the original again afterwards."""
    scene = _make_scene()
    original = scene.run_method
    with batch_scene(scene):
        patched = scene.run_method
        scene.run_method("move", 1.0, 2.0, 3.0)
        with batch_scene(scene):
            assert scene.run_method is patched
            scene.run_method("rotate", 0.1, 0.2, 0.3)
        assert scene.run_method is patched
        scene.run_method("material", "#ff0000", 0.5)

    assert scene.client.run_javascript.call_count == 1
    payload = scene.client.run_javascript.call_args.args[0]
    assert (
        payload.index('"move"')
        < payload.index('"rotate"')
        < payload.index('"material"')
    )
    assert json.dumps(scene.id) in payload
    assert scene.run_method is original
    assert scene._wc_batched_calls is None

    empty = _make_scene()
    with batch_scene(empty):
        pass
    assert empty.client.run_javascript.call_count == 0


def test_a_failing_or_awaited_batch_still_flushes_and_restores() -> None:
    """An exception inside the block still restores run_method and flushes
    what was queued before the raise; awaiting a batched call raises at
    once instead of hanging."""
    scene = _make_scene()
    original = scene.run_method

    with pytest.raises(RuntimeError, match="body kaboom"):
        with batch_scene(scene):
            scene.run_method("move", 1.0, 2.0, 3.0)
            raise RuntimeError("body kaboom")

    assert scene.run_method is original
    assert scene._wc_batched_calls is None
    assert scene.client.run_javascript.call_count == 1
    assert '"move"' in scene.client.run_javascript.call_args.args[0]

    with pytest.raises(BatchedAwaitError, match="fire-and-forget"):
        # __await__ is what `await` desugars to; iterate it directly so
        # we don't need an event loop.
        next(_BATCHED_NULL.__await__())
