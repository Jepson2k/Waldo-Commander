"""Discover skill callables and generate explicit, reproducible Python calls."""

from __future__ import annotations

import asyncio
import ast
import inspect
import keyword
import math
import re
from dataclasses import dataclass
from typing import Any, get_type_hints

from waldoctl import PathSegment, Robot, ToolAction
from waldoctl.setup import Pose, SetupSnapshot, validate_name
from waldoctl.camera import CameraCalibration
from waldoctl.skills import Skill, discover_skills
from waldoctl.signals import DigitalSignal

from waldo_commander.skills.signals import SignalFixture
from waldo_commander.camera_sources import CommanderCameraSource, FrameSource
from waldo_commander.vision import LocalizationLimits

#: Argument types a call takes from the program's setup by name.
SETUP_TYPES = (Pose, SetupSnapshot, DigitalSignal, CameraCalibration)
SETUP_IMPORT = "from waldo_commander.setup import load_setup"
CAMERA_SOURCE_IMPORT = (
    "from waldo_commander.camera_sources import CommanderCameraSource"
)


@dataclass(frozen=True)
class SkillEntry:
    skill: Skill[Any, ..., Any]
    unavailable: str = ""

    @property
    def description(self) -> str:
        return inspect.getdoc(self.skill.function) or self.skill.spec.id

    @property
    def parameters(self) -> dict[str, inspect.Parameter]:
        return dict(list(inspect.signature(self.skill.function).parameters.items())[1:])


def library(robot: Robot | None) -> tuple[dict[str, SkillEntry], list[str]]:
    """Every discovered skill, marked unavailable where *robot* lacks a
    capability it requires; without a backend every requirement is missing."""
    diagnostics: list[str] = []
    skills = discover_skills(diagnostics=diagnostics)
    entries = {}
    for identity, candidate in skills.items():
        missing = candidate.spec.requires.missing_from(robot)
        unavailable = f"Backend lacks: {', '.join(sorted(missing))}" if missing else ""
        entries[identity] = SkillEntry(candidate, unavailable)
    return entries, diagnostics


def _literal(value: Any, imports: set[str]) -> str:
    if isinstance(value, CameraCalibration):
        imports.add("from waldoctl.camera import CameraCalibration")
        return f"CameraCalibration.from_dict({value.to_dict()!r})"
    if isinstance(value, CommanderCameraSource):
        if value.endpoint is not None or value.token is not None:
            raise ValueError(
                "Camera session credentials cannot be exported; use CommanderCameraSource()"
            )
        imports.add(CAMERA_SOURCE_IMPORT)
        return "CommanderCameraSource()"
    if isinstance(value, LocalizationLimits):
        from dataclasses import asdict

        imports.add("from waldo_commander.vision import LocalizationLimits")
        return f"LocalizationLimits(**{asdict(value)!r})"
    if isinstance(value, DigitalSignal):
        imports.add("from waldoctl.signals import DigitalSignal")
        return f"DigitalSignal(**{value.to_dict()!r})"
    if isinstance(value, SignalFixture):
        imports.add("from waldo_commander.skills.signals import SignalFixture")
        return f"SignalFixture({value.value!r})"
    if isinstance(value, Pose):
        return f"Pose({value.values!r}, frame={value.frame!r})"
    if isinstance(value, SetupSnapshot):
        return f"SetupSnapshot.from_dict({value.to_dict()!r})"
    if value is None or type(value) in (str, bool, int):
        return repr(value)
    if type(value) is float and math.isfinite(value):
        return repr(value)
    if isinstance(value, (list, tuple)):
        parts = ", ".join(_literal(item, imports) for item in value)
        return (
            f"[{parts}]"
            if isinstance(value, list)
            else f"({parts},)"
            if value
            else "()"
        )
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return (
            "{"
            + ", ".join(
                f"{key!r}: {_literal(item, imports)}" for key, item in value.items()
            )
            + "}"
        )
    raise ValueError(
        f"Supply {type(value).__name__} directly in Python; it cannot be exported as a fixed call"
    )


def alias(entry: SkillEntry) -> str:
    """The name a program imports *entry* under, one per skill id."""
    return "_skill_" + re.sub(r"[^A-Za-z0-9_]", "_", entry.skill.spec.id)


def _import(entry: SkillEntry) -> tuple[str, str]:
    """The import line for *entry* and the dotted name it is called by."""
    function = entry.skill.function
    module = function.__module__
    qualified_name = getattr(function, "__qualname__", None)
    if not isinstance(qualified_name, str):
        raise ValueError("This callable has no importable Python name")
    parts = qualified_name.split(".")
    if not all(
        part.isidentifier() and not keyword.iskeyword(part)
        for part in [*module.split("."), *parts]
    ):
        raise ValueError(
            "This skill is defined inside a local scope. Move it into an importable module to insert or record it."
        )
    return (
        f"from {module} import {parts[0]} as {alias(entry)}",
        ".".join([alias(entry), *parts[1:]]),
    )


def call_source(
    entry: SkillEntry, arguments: dict[str, Any], *, async_call: bool = False
) -> str:
    """Build a call with typed fixed values; never evaluate user expressions."""
    if entry.unavailable:
        raise ValueError(entry.unavailable)
    inspect.signature(entry.skill.function).bind(object(), **arguments)
    import_line, callable_name = _import(entry)
    imports = {"from waldoctl.setup import Pose, SetupSnapshot"}
    kwargs = ", ".join(
        f"{name}={_literal(value, imports)}" for name, value in arguments.items()
    )
    call = f"{callable_name}{'.async_call' if async_call else ''}(rbt{', ' if kwargs else ''}{kwargs})"
    prelude = "\n".join(sorted(imports))
    return f"{import_line}\n{prelude}\n{'await ' if async_call else ''}{call}"


def skill_prelude(
    entry: SkillEntry,
    setup_variable: str,
    setup_name: str | None,
    annotations: dict[str, Any],
) -> list[str]:
    """The statements a call of *entry* needs above it: the skill's import,
    the setup load when an argument comes from a setup (``None`` for
    *setup_name* when the program loads one already), and the camera
    source's import when a frame source is passed."""
    lines = [_import(entry)[0]]
    kinds = [
        annotations.get(name, parameter.annotation)
        for name, parameter in entry.parameters.items()
    ]
    if setup_name is not None and any(kind in SETUP_TYPES for kind in kinds):
        lines += [
            SETUP_IMPORT,
            f'{setup_variable} = load_setup("{validate_name(setup_name)}")',
        ]
    if FrameSource in kinds:
        lines.append(CAMERA_SOURCE_IMPORT)
    return lines


def _chosen(argument: str, names) -> str:
    """The argument's own name when the setup has it, else the setup's first."""
    return argument if argument in names else next(iter(names), argument)


def default_text(
    name: str,
    annotation: Any,
    default: Any,
    snapshot: SetupSnapshot | None,
    setup_variable: str = "setup",
) -> str:
    """The text an argument starts as: a setup reference by name for setup
    types, the parameter's default otherwise, and the parameter's own name
    where there is no default to write."""
    if annotation is Pose:
        pose = _chosen(name, snapshot.poses if snapshot is not None else ())
        return f'{setup_variable}.resolve("{pose}")'
    if annotation is SetupSnapshot:
        return setup_variable
    if annotation is DigitalSignal:
        signal = _chosen(name, snapshot.signals if snapshot is not None else ())
        return f'{setup_variable}.signals["{signal}"]'
    if annotation is CameraCalibration:
        camera = _chosen(name, snapshot.cameras if snapshot is not None else ())
        return f'{setup_variable}.cameras["{camera}"]'
    if annotation is FrameSource:
        return "CommanderCameraSource()"
    if default is inspect.Parameter.empty:
        return name
    return repr(default)


def _escaped(text: str) -> str:
    return text.replace("{", "\\{").replace("}", "\\}")


def call_template(
    entry: SkillEntry,
    snapshot: SetupSnapshot | None,
    async_call: bool,
    *,
    setup_variable: str = "setup",
) -> tuple[str, str, list[str]]:
    """``(template, plain_text, field_names)`` for a call of *entry*.

    The template makes each argument a snippet field, numbered in signature
    order; the plain text is what the template reads once filled in. An
    argument whose text holds a closing brace cannot be a snippet field and
    stays plain text.
    """
    annotations = get_type_hints(entry.skill.function)
    head = ("await " if async_call else "") + _import(entry)[1]
    head += ".async_call(rbt" if async_call else "(rbt"
    template, plain, fields = [_escaped(head)], [head], []
    for name, parameter in entry.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        text = default_text(
            name,
            annotations.get(name, parameter.annotation),
            parameter.default,
            snapshot,
            setup_variable,
        )
        plain.append(f", {name}={text}")
        if "}" in text:
            template.append(_escaped(f", {name}={text}"))
        else:
            fields.append(name)
            template.append(f", {name}=${{{len(fields)}:{_escaped(text)}}}")
    template.append(")")
    plain.append(")")
    return "".join(template), "".join(plain), fields


@dataclass(frozen=True)
class SkillCall:
    """A skill call written on one line, with where each argument sits in it."""

    key: str
    entry: SkillEntry
    async_call: bool
    #: Argument name → ``(start, end)`` str indices of its text in the line.
    arguments: dict[str, tuple[int, int]]
    nodes: dict[str, ast.expr]
    #: Index of the call's closing parenthesis in the line.
    close: int
    expanded: bool = False


def parse_skill_call(line: str, entries: dict[str, SkillEntry]) -> SkillCall | None:
    """The skill call *line* holds, called by its import alias, or ``None``."""
    if "_skill_" not in line:
        return None
    indent = len(line) - len(line.lstrip())
    code = line[indent:]
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    if len(tree.body) != 1:
        return None
    statement = tree.body[0]
    value = (
        statement.value
        if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign))
        else None
    )
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return None
    function = value.func
    async_call = isinstance(function, ast.Attribute) and function.attr == "async_call"
    if isinstance(function, ast.Attribute) and async_call:
        function = function.value
    keys = {alias(entry): key for key, entry in entries.items()}
    if not isinstance(function, ast.Name) or function.id not in keys:
        return None
    key = keys[function.id]
    entry = entries[key]
    encoded = code.encode()

    def index(offset: int | None) -> int:
        return indent + len(encoded[: offset or 0].decode())

    order = list(entry.parameters)
    nodes: dict[str, ast.expr] = {}
    for position, node in enumerate(value.args[1:]):
        if position < len(order) and not isinstance(node, ast.Starred):
            nodes[order[position]] = node
    for argument in value.keywords:
        if argument.arg is not None:
            nodes[argument.arg] = argument.value
    return SkillCall(
        key,
        entry,
        async_call,
        {
            name: (index(node.col_offset), index(node.end_col_offset))
            for name, node in nodes.items()
        },
        nodes,
        index(value.end_col_offset) - 1,
        any(isinstance(arg, ast.Starred) for arg in value.args)
        or any(arg.arg is None for arg in value.keywords),
    )


def field_at(call: SkillCall, index: int) -> str | None:
    """The argument whose text holds str index *index* of the line, ends included."""
    return next(
        (
            name
            for name, (start, end) in call.arguments.items()
            if start <= index <= end
        ),
        None,
    )


def replace_argument(line: str, call: SkillCall, name: str, text: str) -> str:
    """*line* with argument *name* written as *text*, added if the call lacks it."""
    if name in call.arguments:
        start, end = call.arguments[name]
        return line[:start] + text + line[end:]
    head = line[: call.close].rstrip()
    separator = "" if head.endswith("(") else " " if head.endswith(",") else ", "
    return f"{head}{separator}{name}={text}{line[call.close :]}"


def _is_setup_member(node: ast.AST, setup_variable: str, member: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == member
        and isinstance(node.value, ast.Name)
        and node.value.id == setup_variable
    )


def _entry_name(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise ValueError(f"Name the setup entry with a string, not {ast.unparse(node)}")


def setup_reference(node: ast.AST, setup_variable: str = "setup") -> str | None:
    """The setup entry *node* names: ``setup.resolve("x")``,
    ``setup.signals["x"]`` or ``setup.cameras["x"]`` give ``x``."""
    if (
        isinstance(node, ast.Call)
        and _is_setup_member(node.func, setup_variable, "resolve")
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    if (
        isinstance(node, ast.Subscript)
        and any(
            _is_setup_member(node.value, setup_variable, member)
            for member in ("signals", "cameras")
        )
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def _value(node: ast.AST, snapshot: SetupSnapshot | None, setup_variable: str) -> Any:
    if isinstance(node, ast.Call) and (
        any(isinstance(arg, ast.Starred) for arg in node.args)
        or any(arg.arg is None for arg in node.keywords)
    ):
        raise ValueError("The preview reads fixed values, not expanded arguments")

    def setup() -> SetupSnapshot:
        if snapshot is None:
            raise ValueError(
                f"The program loads no setup as {setup_variable!r} to read this from"
            )
        return snapshot

    def entry(kind: str, key: str):
        entries = getattr(setup(), kind)
        if key not in entries:
            raise ValueError(f"The setup has no {kind[:-1]} {key!r}")
        return entries[key]

    if isinstance(node, ast.Constant) and (
        node.value is None or type(node.value) in (bool, int, float, str)
    ):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        number = node.operand.value
        return -number if isinstance(node.op, ast.USub) else number
    if isinstance(node, (ast.Tuple, ast.List)):
        items = [_value(item, snapshot, setup_variable) for item in node.elts]
        return tuple(items) if isinstance(node, ast.Tuple) else items
    if isinstance(node, ast.Name) and node.id == setup_variable:
        return setup()
    if (
        isinstance(node, ast.Call)
        and _is_setup_member(node.func, setup_variable, "resolve")
        and len(node.args) == 1
        and not node.keywords
    ):
        return setup().resolve(_entry_name(node.args[0]))
    for kind in ("signals", "cameras"):
        if isinstance(node, ast.Subscript) and _is_setup_member(
            node.value, setup_variable, kind
        ):
            return entry(kind, _entry_name(node.slice))
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.value, ast.Subscript)
        and _is_setup_member(node.value.value, setup_variable, "parameters")
    ):
        return entry("parameters", _entry_name(node.value.slice)).value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "Pose":
            return Pose(
                *(_value(item, snapshot, setup_variable) for item in node.args),
                **{
                    item.arg: _value(item.value, snapshot, setup_variable)
                    for item in node.keywords
                    if item.arg is not None
                },
            )
        if node.func.id == "CommanderCameraSource" and not (node.args or node.keywords):
            return CommanderCameraSource()
    raise ValueError(
        f"The preview reads fixed values and setup entries, not {ast.unparse(node)}"
    )


def arguments_from_call(
    call: SkillCall, snapshot: SetupSnapshot | None, setup_variable: str = "setup"
) -> dict[str, Any]:
    """The call's arguments as values, without running any of the program.

    Only a closed grammar is read: constants, tuples and lists of them, the
    setup itself, its poses, signals, cameras and parameter values by name,
    ``Pose(...)`` and ``CommanderCameraSource()``.
    """
    if call.expanded:
        raise ValueError("The preview reads fixed values, not expanded arguments")
    return {
        name: _value(node, snapshot, setup_variable)
        for name, node in call.nodes.items()
    }


async def plan_preview(
    entry: SkillEntry,
    arguments: dict[str, Any],
    robot: Robot,
    joints_rad: Any,
    tool: tuple[str, str],
) -> tuple[list[PathSegment], list[ToolAction]]:
    """Plan a call of *entry* with *arguments* from *joints_rad*, without the robot.

    The plan runs the generated source itself, so what is drawn is exactly
    what Insert would put in the program. That source is the skill's own
    code, and a dry run's world and tool are process-wide, so it runs in the
    program-preview worker under the program preview's time limit.
    """
    import waldoctl
    from nicegui import run

    from waldo_commander.services.path_visualizer import _simulation_timeout_s

    source = call_source(entry, arguments)
    if run.process_pool is None:
        raise RuntimeError("Preview unavailable: no simulation process pool")
    scene = waldoctl.commander.scene
    shapes_wire = [s.to_wire() for s in scene.shapes] if scene is not None else []
    try:
        async with asyncio.timeout(_simulation_timeout_s() + 2.0):
            planned = await run.cpu_bound(
                _plan_isolated,
                source,
                robot.backend_package,
                joints_rad,
                tool,
                shapes_wire,
            )
    except TimeoutError:
        planned = None
    except run.SubprocessException as error:
        # Its text carries the worker's whole traceback; the form shows a sentence.
        raise RuntimeError(error.original_message) from None
    # cpu_bound answers None when its wait is cancelled: by the limit, or at shutdown.
    if planned is None:
        raise TimeoutError("planning it took too long")
    return planned


def _plan_isolated(
    source: str,
    backend_package: str,
    joints_rad: Any,
    tool: tuple[str, str],
    shapes_wire: list[tuple],
) -> tuple[list[PathSegment], list[ToolAction]]:
    """Run a generated call against a dry run; :func:`plan_preview` in a worker."""
    from waldoctl import shape_from_wire

    from waldo_commander.profiles import get_robot
    from waldo_commander.services.path_preview_client import PathPreviewClient
    from waldo_commander.services.path_visualizer import _tool_metadata
    from waldo_commander.services.preview_segments import (
        segments_from_record,
        tool_actions_from_record,
    )

    robot = get_robot(backend_package)
    # The worker is shared, so it still holds the last preview's world.
    if robot.has_collision_checking:
        robot.apply_shapes([shape_from_wire(*wire) for wire in shapes_wire])
    client = PathPreviewClient(
        dry_run_client_cls=lambda **kw: robot.create_dry_run_client(**kw),
        initial_joints=joints_rad,
        tool_meta_registry=_tool_metadata(robot),
        robot=robot,
    )
    key, variant = tool
    if key not in ("", "NONE") and client.select_tool(key, variant_key=variant) < 0:
        raise RuntimeError(f"The preview refused tool {key}")
    exec(source, {"rbt": client})
    client.close()
    if client.accumulated_errors:
        raise RuntimeError("; ".join(client.accumulated_errors))
    record = client.plan()
    segments = segments_from_record(record, client.notes)
    return segments, tool_actions_from_record(
        client.tool_action_collector, record, segments
    )
