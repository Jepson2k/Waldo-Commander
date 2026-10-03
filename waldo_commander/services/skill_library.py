"""Discover skill callables and generate explicit, reproducible Python calls."""

from __future__ import annotations

import asyncio
import inspect
import keyword
import math
import re
from dataclasses import dataclass
from typing import Any

from waldoctl import PathSegment, Robot, ToolAction
from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.skills import Skill, discover_skills
from waldoctl.signals import DigitalSignal

from waldo_commander.skills.signals import SignalFixture


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


def call_source(
    entry: SkillEntry, arguments: dict[str, Any], *, async_call: bool = False
) -> str:
    """Build a call with typed fixed values; never evaluate user expressions."""
    candidate = entry.skill
    if entry.unavailable:
        raise ValueError(entry.unavailable)
    signature = inspect.signature(candidate.function)
    signature.bind(object(), **arguments)
    module = candidate.function.__module__
    qualified_name = getattr(candidate.function, "__qualname__", None)
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
    alias = "_skill_" + re.sub(r"[^A-Za-z0-9_]", "_", candidate.spec.id)
    callable_name = ".".join([alias, *parts[1:]])
    imports = {"from waldoctl.setup import Pose, SetupSnapshot"}
    kwargs = ", ".join(
        f"{name}={_literal(value, imports)}" for name, value in arguments.items()
    )
    call = f"{callable_name}{'.async_call' if async_call else ''}(rbt{', ' if kwargs else ''}{kwargs})"
    prelude = "\n".join(sorted(imports))
    return f"from {module} import {parts[0]} as {alias}\n{prelude}\n{'await ' if async_call else ''}{call}"


async def plan_preview(
    entry: SkillEntry,
    arguments: dict[str, Any],
    robot: Robot,
    joints_rad: Any,
    tool: tuple[str, str],
) -> tuple[list[PathSegment], list[ToolAction]]:
    """Plan the call this panel would insert, from *joints_rad*, without the robot.

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
    segments, tool_actions = planned
    return [PathSegment.from_dict(segment) for segment in segments], tool_actions


def _plan_isolated(
    source: str,
    backend_package: str,
    joints_rad: Any,
    tool: tuple[str, str],
    shapes_wire: list[tuple],
) -> tuple[list[dict], list[ToolAction]]:
    """Run a generated call against a dry run; :func:`plan_preview` in a worker."""
    from waldoctl import shape_from_wire

    from waldo_commander.profiles import get_robot
    from waldo_commander.services.path_preview_client import PathPreviewClient
    from waldo_commander.services.path_visualizer import _tool_metadata

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
    return list(client.segment_collector), list(client.tool_action_collector)
