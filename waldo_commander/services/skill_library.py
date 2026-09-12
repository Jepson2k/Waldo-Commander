"""Discover skill callables and generate explicit, reproducible Python calls."""

from __future__ import annotations

import inspect
import keyword
import math
import re
from dataclasses import dataclass
from typing import Any

from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.camera import CameraCalibration
from waldoctl.skills import Skill, discover_skills
from waldoctl.signals import DigitalSignal

from waldo_commander.skills.signals import SignalFixture
from waldo_commander.camera_sources import CommanderCameraSource
from waldo_commander.vision import LocalizationLimits


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


def library(capabilities: frozenset[str]) -> tuple[dict[str, SkillEntry], list[str]]:
    diagnostics: list[str] = []
    skills = discover_skills(diagnostics=diagnostics)
    entries = {}
    for identity, candidate in skills.items():
        missing = candidate.spec.requires - capabilities
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
        imports.add("from waldo_commander.camera_sources import CommanderCameraSource")
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
