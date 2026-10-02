"""Robot command discovery via docstring introspection.

Scans a backend's client and tool classes for methods with ``Category:``
and ``Example:`` docstring sections, producing command metadata for the
editor's command palette and autocompletion.
"""

import inspect
import logging
import re

from nicegui.elements.codemirror.codemirror import CompletionItem
from waldoctl.setup import SetupSnapshot

from waldo_commander.state import ui_state

logger = logging.getLogger(__name__)

_CATEGORY_RE = re.compile(r"^\s*Category:\s*(.+)", re.MULTILINE)
_EXAMPLE_RE = re.compile(r"^\s*Examples?:\s*$", re.MULTILINE)

# Cached robot commands (populated lazily, never invalidated — backend
# switching requires an app restart).
_robot_commands_cache: dict | None = None


def _parse_docstring_category(doc: str) -> str | None:
    """Extract ``Category: Foo`` from a Google-style docstring."""
    m = _CATEGORY_RE.search(doc)
    return m.group(1).strip() if m else None


def _parse_docstring_example(doc: str) -> str | None:
    """Extract the first indented line after an ``Example:`` section."""
    m = _EXAMPLE_RE.search(doc)
    if not m:
        return None
    rest = doc[m.end() :]
    for line in rest.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _scan_class_commands(cls: type, prefix: str = "") -> dict:
    """Scan a class for methods with ``Category:`` and ``Example:`` docstring sections.

    Returns a dict of ``{method_name: command_info}`` where method_name
    includes the optional prefix (e.g. ``"tool.open"``).
    Uses ``inspect.getdoc()`` to walk the MRO for inherited docstrings.
    """
    commands = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        attr = getattr(cls, name, None)
        if not callable(attr):
            continue

        doc = (inspect.getdoc(attr) or "").strip()
        category = _parse_docstring_category(doc)
        snippet = _parse_docstring_example(doc)
        if category is None or snippet is None:
            continue

        key = f"{prefix}{name}" if prefix else name
        sig = inspect.signature(attr)
        first_line = doc.splitlines()[0] if doc else ""

        commands[key] = {
            "title": f"rbt.{key}(...)",
            "category": category,
            "snippet": snippet,
            "signature": str(sig),
            "docstring": first_line or "No description available",
        }

    return commands


def discover_robot_commands() -> dict:
    """Introspect the active backend's client and tool classes for available commands (cached).

    Only methods whose docstrings contain both ``Category:`` and ``Example:``
    sections are included.  Methods without these sections are silently excluded.
    """
    global _robot_commands_cache
    if _robot_commands_cache is not None:
        return _robot_commands_cache

    commands = {}

    # Client methods (rbt.move_j, rbt.home, etc.)
    try:
        client_cls = ui_state.active_robot.async_client_class
        commands.update(_scan_class_commands(client_cls))
    except (AttributeError, RuntimeError, AssertionError):
        logger.warning("Could not get async_client_class for command discovery")

    # Tool methods (rbt.tool.open, rbt.tool.close, etc.)
    # Scan all tool specs — different implementations may expose different
    # methods or override docstrings differently.  First discovery wins.
    try:
        for spec in ui_state.active_robot.tools.available:
            if spec.key == "NONE":
                continue
            for k, v in _scan_class_commands(type(spec), prefix="tool.").items():
                commands.setdefault(k, v)
    except (AttributeError, RuntimeError):
        pass

    _robot_commands_cache = commands
    return commands


def generate_completions_from_commands() -> list[CompletionItem]:
    """Generate CodeMirror completion items from discovered robot commands."""
    all_commands = discover_robot_commands()
    completions: list[CompletionItem] = []

    for name, cmd in all_commands.items():
        sig = cmd["signature"]
        sig_clean = sig.replace("(self, ", "(").replace("(self)", "()")

        completion: CompletionItem = {
            "label": f"rbt.{name}",
            "detail": sig_clean,
            "info": cmd["docstring"],
            "apply": f"rbt.{name}",
            "type": "function",
        }
        completions.append(completion)

    from waldoctl.skills import discover_skills

    skills = discover_skills().values()
    trailing: dict[str, int] = {}
    for candidate in skills:
        trailing[getattr(candidate.function, "__name__", "")] = (
            trailing.get(getattr(candidate.function, "__name__", ""), 0) + 1
        )
    for candidate in skills:
        name = getattr(candidate.function, "__name__", None)
        if not isinstance(name, str):
            continue
        # Two plugins can provide the same function name; an unqualified
        # completion list would offer them as one entry and insert whichever
        # was discovered first.
        label = f"{name} ({candidate.spec.id})" if trailing[name] > 1 else name
        completions.append(
            {
                "label": label,
                "detail": str(inspect.signature(candidate.function)),
                "info": f"{candidate.spec.id} · Import from {candidate.function.__module__}. {inspect.getdoc(candidate.function) or ''}",
                "apply": name,
                "type": "function",
            }
        )

    return completions


def setup_completions(
    snapshot: SetupSnapshot, variable: str = "setup"
) -> list[CompletionItem]:
    """Completions for the entries of the setup a program loads as *variable*.

    A pose is offered by its reference and by its bare name, which completes
    to the reference, so typing the name a pose was taught under finds it.
    """
    items: list[CompletionItem] = []
    for name, pose in snapshot.poses.items():
        reference = f'{variable}.resolve("{name}")'
        detail = f"pose in {pose.frame}"
        items.append(
            {
                "label": reference,
                "detail": detail,
                "type": "variable",
                "section": "Setup",
            }
        )
        items.append(
            {
                "label": name,
                "apply": reference,
                "detail": detail,
                "type": "variable",
                "section": "Setup",
            }
        )
    for name, frame in snapshot.frames.items():
        items.append(
            {
                "label": f'{variable}.frames["{name}"]',
                "detail": f"frame in {frame.parent}",
                "type": "variable",
                "section": "Setup",
            }
        )
    for name in snapshot.signals:
        items.append(
            {
                "label": f'{variable}.signals["{name}"]',
                "detail": "signal",
                "type": "variable",
                "section": "Setup",
            }
        )
    for name in snapshot.cameras:
        items.append(
            {
                "label": f'{variable}.cameras["{name}"]',
                "detail": "camera calibration",
                "type": "variable",
                "section": "Setup",
            }
        )
    for name, parameter in snapshot.parameters.items():
        items.append(
            {
                "label": f'{variable}.parameters["{name}"].value',
                "detail": f"{parameter.value!r} {parameter.unit}".strip(),
                "type": "constant",
                "section": "Setup",
            }
        )
    return items
