"""Waldo Commander's 3D view: a three.js scene drawn from ``Node`` objects.

``WcScene`` is the view. Objects are made with its methods (``scene.box()``)
or with this module's functions, which draw into the node or scene of the
enclosing ``with`` block (``with group: s3d.box()``).
"""

from __future__ import annotations

from typing import Any

from .element import ClipPlane, Node, WcScene
from .node import current_parent


def _scene() -> WcScene:
    parent = current_parent.get()
    if parent is None:
        raise RuntimeError(
            "no 3D scene to draw into: create objects inside `with scene:`"
        )
    return parent.scene


def group() -> Node:
    return _scene().group()


def box(*args: Any, **kwargs: Any) -> Node:
    return _scene().box(*args, **kwargs)


def sphere(*args: Any, **kwargs: Any) -> Node:
    return _scene().sphere(*args, **kwargs)


def cylinder(*args: Any, **kwargs: Any) -> Node:
    return _scene().cylinder(*args, **kwargs)


def capsule(*args: Any, **kwargs: Any) -> Node:
    return _scene().capsule(*args, **kwargs)


def lathe(*args: Any, **kwargs: Any) -> Node:
    return _scene().lathe(*args, **kwargs)


def line(*args: Any, **kwargs: Any) -> Node:
    return _scene().line(*args, **kwargs)


def polyline(*args: Any, **kwargs: Any) -> Node:
    return _scene().polyline(*args, **kwargs)


def stl(*args: Any, **kwargs: Any) -> Node:
    return _scene().stl(*args, **kwargs)


__all__ = [
    "ClipPlane",
    "Node",
    "WcScene",
    "box",
    "capsule",
    "cylinder",
    "group",
    "lathe",
    "line",
    "polyline",
    "sphere",
    "stl",
]
