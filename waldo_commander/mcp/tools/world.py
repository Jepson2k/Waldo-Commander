"""MCP tools for the collision world — ``world.*``.

The world an LLM edits here is the one the GUI shows and the backend
enforces: every mutation reassigns ``commander.scene.shapes`` (the request /
readback path a program's ``set_shapes`` uses), so the backend push, draft
styling, program recording and local collision checking all apply. The
installation layer is the robot config's and read-only here — the floor
is one of its shapes, not a separate thing; the
export tool renders shapes as the TOML that config declares them with.

Mutations need the control lease — of the enforced world and of the saved
library alike, since both are the human's workspace; changing the world is
not actuation, so no hardware consent is asked. Reads never do. Refusals are
ToolErrors at WARNING — protocol messages steering the LLM, not server faults.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, NoReturn

import waldoctl
from fastmcp.exceptions import ToolError
from waldoctl.shapes import Shape, ShapeWorld, shape_from_wire
from waldoctl.world import world_from_dict, world_to_dict

from waldo_commander.mcp.server import get_mcp
from waldo_commander.mcp.tools.control import require_control
from waldo_commander.services import world_files

mcp = get_mcp()


def _refuse(message: str) -> NoReturn:
    raise ToolError(message, log_level=logging.WARNING)


def _scene() -> Any:
    scene = waldoctl.commander.scene
    if scene is None:
        _refuse("this host has no 3D scene, so there is no world to edit")
    return scene


def _world(scene: Any) -> ShapeWorld:
    return ShapeWorld(
        installation=tuple(scene.installation),
        program=tuple(scene.shapes),
    )


def _snapshot(scene: Any) -> dict:
    """The world as the JSON document import/export and the library use,
    plus whether the displayed program layer matches backend readback and
    the shapes proposed for the installation layer (drawn, not enforced)."""
    draft = ShapeWorld(program=tuple(scene.installation_draft))
    return {
        **world_to_dict(_world(scene)),
        "confirmed": bool(scene.confirmed),
        "installation_draft": world_to_dict(draft)["program"],
    }


def _parse(entry: Any) -> Shape:
    """One shape from its wire form — the 7-item list ``Shape.to_wire()``
    yields, or the same fields as a dict."""
    try:
        if isinstance(entry, dict):
            return shape_from_wire(**entry)
        return shape_from_wire(*entry)
    except (TypeError, ValueError) as err:
        _refuse(f"shape rejected: {err}")


def _require_free_name(scene: Any, name: str, *, replacing: str | None = None) -> None:
    """Refuse a name already drawn in either layer. Every consumer — the
    scene, the collision report, remove_shape — keys on the name, so a
    duplicate is ambiguous everywhere. *replacing* is the program shape
    about to be overwritten, whose own name is free to reuse."""
    taken = {s.name for s in scene.shapes if s.name != replacing}
    taken |= {s.name for s in scene.installation}
    taken |= {s.name for s in scene.installation_draft}
    if name in taken:
        _refuse(f"a shape named {name!r} already exists")


def _library_entry(name: str) -> ShapeWorld:
    """A library entry, or a refusal the LLM can act on — the file is
    user-editable, so malformed JSON is expected input, not a fault."""
    try:
        return world_files.load_entry(name)
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as err:
        _refuse(f"library entry unavailable: {err}")


def _apply(scene: Any, shapes: list[Shape]) -> dict:
    """Reassign the program layer whole — the handle's contract — and report."""
    try:
        scene.shapes = shapes
    except ValueError as err:
        _refuse(f"world rejected: {err}")
    return _snapshot(scene)


@mcp.tool(name="world.get")
async def get_world() -> dict:
    """The collision world as displayed: installation layer (robot config,
    the floor among its shapes), program layer, and whether the program
    layer is confirmed by backend readback. Shapes are 7-item wire rows
    ``[kind, params, pose, collision, margin, name, physics]`` in metres and
    radians; the same document ``world.import_world`` accepts."""
    return _snapshot(_scene())


@mcp.tool(name="world.set_shapes")
async def set_shapes(shapes: list[Any]) -> dict:
    """Replace the program-layer keep-outs and objects with *shapes* (wire
    rows or field dicts). The installation layer is untouched — it is config."""
    require_control()
    scene = _scene()
    return _apply(scene, [_parse(s) for s in shapes])


@mcp.tool(name="world.add_shape")
async def add_shape(shape: Any) -> dict:
    """Add one shape to the program layer; its name must be new."""
    require_control()
    scene = _scene()
    new = _parse(shape)
    _require_free_name(scene, new.name)
    return _apply(scene, [*scene.shapes, new])


@mcp.tool(name="world.update_shape")
async def update_shape(name: str, shape: Any) -> dict:
    """Replace the program-layer shape called *name* with *shape* (which may
    rename it)."""
    require_control()
    scene = _scene()
    if not any(s.name == name for s in scene.shapes):
        _refuse(f"no program-layer shape named {name!r}")
    new = _parse(shape)
    _require_free_name(scene, new.name, replacing=name)
    return _apply(scene, [new if s.name == name else s for s in scene.shapes])


@mcp.tool(name="world.remove_shape")
async def remove_shape(name: str) -> dict:
    """Remove the program-layer shape (or object) called *name*."""
    require_control()
    scene = _scene()
    if not any(s.name == name for s in scene.shapes):
        _refuse(f"no program-layer shape named {name!r}")
    return _apply(scene, [s for s in scene.shapes if s.name != name])


@mcp.tool(name="world.import_world")
async def import_world(world: dict) -> dict:
    """Apply a world document (``world.get`` / ``world.export`` form, or a
    library entry) as the program layer. Its installation entries are not
    applied — that layer is the robot config's — and ``installation_matches``
    reports whether they equal the live one."""
    require_control()
    scene = _scene()
    try:
        parsed = world_from_dict(world)
    except (TypeError, ValueError, KeyError) as err:
        _refuse(f"world document rejected: {err}")
    out = _apply(scene, list(parsed.program))
    out["installation_matches"] = tuple(parsed.installation) == tuple(
        scene.installation
    )
    return out


@mcp.tool(name="world.export")
async def export_world() -> dict:
    """The world as a document to save or store in the library."""
    return world_to_dict(_world(_scene()))


@mcp.tool(name="world.library_list")
async def library_list() -> list[str]:
    """Names of the saved library entries (world documents on disk)."""
    return world_files.list_entries()


@mcp.tool(name="world.library_save")
async def library_save(name: str, shapes: list[Any] | None = None) -> str:
    """Save *shapes* (default: the current program layer) as library entry
    *name*, overwriting an entry of that name. Returns the file path."""
    require_control()
    scene = _scene()
    program = (
        tuple(_parse(s) for s in shapes) if shapes is not None else tuple(scene.shapes)
    )
    try:
        return str(world_files.save_entry(name, ShapeWorld(program=program)))
    except (ValueError, OSError) as err:
        _refuse(f"library save failed: {err}")


@mcp.tool(name="world.library_load")
async def library_load(name: str) -> dict:
    """Replace the program layer with library entry *name*."""
    require_control()
    scene = _scene()
    return _apply(scene, list(_library_entry(name).program))


@mcp.tool(name="world.library_delete")
async def library_delete(name: str) -> list[str]:
    """Delete library entry *name*; returns the remaining names."""
    require_control()
    try:
        world_files.delete_entry(name)
    except (ValueError, OSError) as err:
        _refuse(f"library delete failed: {err}")
    return world_files.list_entries()


@mcp.tool(name="world.place_object")
async def place_object(
    entry: str, name: str | None = None, pose: list[float] | None = None
) -> dict:
    """Place a library object into the program layer: *entry* must hold
    exactly one shape, which is added under *name* (default: its own) at
    *pose* ``[x, y, z, rx, ry, rz]`` (default: as saved). A physical entry
    stays physical, so the simulator and preview treat it as a body."""
    require_control()
    scene = _scene()
    saved = _library_entry(entry)
    if len(saved.program) != 1:
        _refuse(
            f"library entry {entry!r} holds {len(saved.program)} shapes; "
            "place_object needs exactly one — use library_load for a whole world"
        )
    shape = saved.program[0]
    changes: dict[str, Any] = {}
    if name is not None:
        changes["name"] = name
    if pose is not None:
        if len(pose) != 6:
            _refuse("pose must be [x, y, z, rx, ry, rz] in metres and radians")
        changes["pose"] = tuple(float(v) for v in pose)
    try:
        placed = dataclasses.replace(shape, **changes)
    except (TypeError, ValueError) as err:
        _refuse(f"object rejected: {err}")
    _require_free_name(scene, placed.name)
    return _apply(scene, [*scene.shapes, placed])


@mcp.tool(name="world.propose_installation")
async def propose_installation(names: list[str]) -> dict:
    """Move the named program-layer shapes into the installation proposal:
    they leave the enforced program layer and are drawn as a proposal until
    the robot config declares them (see ``world.export_installation_toml``)."""
    require_control()
    scene = _scene()
    try:
        scene.propose_installation(list(names))
    except ValueError as err:
        _refuse(f"proposal refused: {err}")
    return _snapshot(scene)


@mcp.tool(name="world.discard_installation_draft")
async def discard_installation_draft(names: list[str] | None = None) -> dict:
    """Drop the named proposed installation shapes (all when omitted)."""
    require_control()
    scene = _scene()
    scene.discard_installation_draft(None if names is None else list(names))
    return _snapshot(scene)


@mcp.tool(name="world.export_installation_toml")
async def export_installation_toml(shapes: list[Any] | None = None) -> str:
    """Render *shapes* — default: the installation proposal, else the current
    program layer — as the robot config's ``[[installation_shapes]]`` TOML,
    the way a designed layout becomes the installation layer the backend
    enforces from boot. Nothing is applied; the text goes into the config."""
    scene = _scene()
    if shapes is not None:
        chosen = [_parse(s) for s in shapes]
    else:
        chosen = list(scene.installation_draft) or list(scene.shapes)
    return world_files.installation_toml(chosen)
