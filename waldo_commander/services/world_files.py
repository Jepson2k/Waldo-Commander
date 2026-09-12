"""World files: the object library on disk and the installation TOML export.

A library entry is a Python module under ``programs/worlds/`` holding one
``ShapeWorld`` literal in ``waldoctl.world``'s dict schema, so a saved world
is copied, diffed and imported like the program that uses it, and the same
codec serves the MCP import/export tools. The
installation export renders shapes as the backend's ``[[installation_shapes]]``
TOML, the form a robot config declares them in, because installation
authoring is config authoring: the GUI and MCP draft it, the config enforces it.
"""

from __future__ import annotations

import json
import keyword
import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path
from pprint import pformat

from waldoctl.shapes import Shape, ShapeWorld
from waldoctl.world import world_from_dict, world_to_dict

from waldo_commander.constants import default_program_dir

logger = logging.getLogger(__name__)

_ENTRY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def library_dir() -> Path:
    """Where library entries live: beside the programs, in ``worlds/``.

    An entry is a Python module holding one ``ShapeWorld`` literal, so a
    saved world is copied, diffed and imported like the program that uses it.
    """
    return default_program_dir() / "worlds"


def _entry_path(name: str) -> Path:
    if not _ENTRY_NAME.match(name):
        raise ValueError(
            f"library entry name {name!r} must be letters, digits, '_', '-' or '.'"
        )
    return library_dir() / f"{name}.py"


def list_entries() -> list[str]:
    root = library_dir()
    if not root.is_dir():
        return []
    _convert_legacy_json(root)
    return sorted(p.stem for p in root.glob("*.py") if _ENTRY_NAME.match(p.stem))


def world_module(world: ShapeWorld, *, variable: str = "world") -> str:
    """Ordinary Python holding the world's shapes, without storage I/O."""
    if not variable.isidentifier() or keyword.iskeyword(variable):
        raise ValueError("World variable must be a Python identifier")
    return (
        "from waldoctl.world import world_from_dict\n\n"
        f"{variable} = world_from_dict({pformat(world_to_dict(world), sort_dicts=True)})\n"
    )


def save_entry(name: str, world: ShapeWorld) -> Path:
    """Write a library entry, replacing any previous one atomically.

    Through a temp file and `os.replace` so an interrupted write cannot
    leave a truncated module behind: the library is the user's own saved
    work, and a half-written world reads back as an import failure.
    """
    path = _entry_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        f'"""World library entry {name!r}, written by Commander; edit freely."""\n\n'
        + world_module(world),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def load_entry(name: str) -> ShapeWorld:
    path = _entry_path(name)
    if not path.is_file():
        _convert_legacy_json(library_dir())
    if not path.is_file():
        raise FileNotFoundError(f"no library entry {name!r} in {path.parent}")
    # Compiled from source, never through the bytecode cache: it is keyed on
    # mtime and size, so a same-second re-save could read back stale shapes.
    namespace: dict[str, object] = {"__name__": f"worlds.{name}", "__file__": str(path)}
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    except Exception as error:
        raise ValueError(
            f"world module {path.name} failed to import: {error}"
        ) from error
    world = namespace.get("world")
    if not isinstance(world, ShapeWorld):
        raise ValueError(
            f"world module {path.name} must define `world = ...` as a ShapeWorld"
        )
    return world


def delete_entry(name: str) -> None:
    path = _entry_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"no library entry {name!r} in {path.parent}")
    path.unlink()


def _convert_legacy_json(root: Path) -> None:
    """A `.json` entry from an earlier release becomes its `.py` twin; the JSON
    goes once the module is on disk, so deleting the entry later does not
    resurrect it."""
    for legacy in root.glob("*.json"):
        target = legacy.with_suffix(".py")
        if target.exists() or not _ENTRY_NAME.match(legacy.stem):
            continue
        try:
            world = world_from_dict(json.loads(legacy.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning(
                "Legacy world entry %s not converted: %s", legacy.name, error
            )
            continue
        save_entry(legacy.stem, world)
        legacy.unlink()


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return repr(float(value))


def installation_toml(shapes: Iterable[Shape]) -> str:
    """The ``[[installation_shapes]]`` blocks a robot config declares *shapes*
    with — the wire fields by name, defaults omitted — ready to paste into
    the robot TOML."""
    blocks = []
    for s in shapes:
        kind, params, pose, collision, margin, name, physics, attachment = s.to_wire()
        if attachment is not None:
            raise ValueError(
                "detach held geometry before exporting installation configuration"
            )
        lines = [
            "[[installation_shapes]]",
            f"name = {_toml_value(name)}",
            f"kind = {_toml_value(kind)}",
            f"params = {_toml_value(params)}",
            f"pose = {_toml_value(pose)}",
        ]
        if not collision:
            lines.append("collision = false")
        if margin is not None:
            lines.append(f"margin = {_toml_value(margin)}")
        if physics is not None:
            mass, friction = physics
            lines.append("")
            lines.append("[installation_shapes.physics]")
            if mass is not None:
                lines.append(f"mass = {_toml_value(mass)}")
            lines.append(f"friction = {_toml_value(friction)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")
