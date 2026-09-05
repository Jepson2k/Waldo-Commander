"""World files: the object library on disk and the installation TOML export.

A library entry *is* a world file — ``waldoctl.world``'s JSON schema, one
file per entry under the program directory — so the same codec serves a
saved world, a library object and the MCP import/export tools. The
installation export renders shapes as the backend's ``[[installation_shapes]]``
TOML, the form a robot config declares them in, because installation
authoring is config authoring: the GUI and MCP draft it, the config enforces it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from waldoctl.shapes import Shape, ShapeWorld
from waldoctl.world import world_from_dict, world_to_dict

from waldo_commander.constants import default_program_dir

_ENTRY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def library_dir() -> Path:
    """Where library entries live: beside the programs, in ``world_library/``."""
    return default_program_dir() / "world_library"


def _entry_path(name: str) -> Path:
    if not _ENTRY_NAME.match(name):
        raise ValueError(
            f"library entry name {name!r} must be letters, digits, '_', '-' or '.'"
        )
    return library_dir() / f"{name}.json"


def list_entries() -> list[str]:
    root = library_dir()
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.json"))


def save_entry(name: str, world: ShapeWorld) -> Path:
    path = _entry_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(world_to_dict(world), indent=2), encoding="utf-8")
    return path


def load_entry(name: str) -> ShapeWorld:
    path = _entry_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"no library entry {name!r} in {path.parent}")
    return world_from_dict(json.loads(path.read_text(encoding="utf-8")))


def delete_entry(name: str) -> None:
    path = _entry_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"no library entry {name!r} in {path.parent}")
    path.unlink()


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
        kind, params, pose, collision, margin, name, physics = s.to_wire()
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
