"""Discover explicit entries without executing code, then verify fresh state."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import math
import time
from dataclasses import dataclass
from typing import Any

from waldoctl.client import RobotClient
from waldoctl.status import ActionState
from waldoctl.restart import is_restart_entry


@dataclass(frozen=True)
class RestartEntry:
    name: str
    description: str
    line: int


def source_digest(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _main_guard(node: ast.If) -> bool:
    test = node.test
    if (
        node.orelse
        or not isinstance(test, ast.Compare)
        or len(test.ops) != 1
        or not isinstance(test.ops[0], ast.Eq)
    ):
        return False
    left, right = test.left, test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    ) or (
        isinstance(right, ast.Name)
        and right.id == "__name__"
        and isinstance(left, ast.Constant)
        and left.value == "__main__"
    )


def _literal(node: ast.expr | None) -> bool:
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError):
        return False


def _annotation(node: ast.expr | None) -> bool:
    return node is None or not any(
        isinstance(part, (ast.Call, ast.NamedExpr, ast.Await, ast.Yield, ast.Lambda))
        for part in ast.walk(node)
    )


def discover_entries(source: str) -> list[RestartEntry]:
    """Parse declarations; imports, decorators and the program body are not run."""
    tree = ast.parse(source)
    decorators = {"waldoctl.restart.restart_entry"}
    skills = {"waldoctl.skills.skill"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "waldoctl.restart":
            decorators.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "restart_entry"
            )
        if isinstance(node, ast.Import):
            decorators.update(
                f"{alias.asname}.restart_entry"
                for alias in node.names
                if alias.name == "waldoctl.restart" and alias.asname
            )
            skills.update(
                f"{alias.asname}.skill"
                for alias in node.names
                if alias.name == "waldoctl.skills" and alias.asname
            )
        if isinstance(node, ast.ImportFrom) and node.module == "waldoctl.skills":
            skills.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "skill"
            )
    entries = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            _dotted(d) in decorators for d in node.decorator_list
        ):
            required = (
                len(node.args.posonlyargs)
                + len(node.args.args)
                - len(node.args.defaults)
            )
            if required or any(v is None for v in node.args.kw_defaults):
                raise ValueError(
                    f"Restart entry {node.name} must be callable without arguments"
                )
            entries.append(
                RestartEntry(
                    node.name,
                    (ast.get_docstring(node) or "").split("\n")[0],
                    node.lineno,
                )
            )
    if not entries:
        return []
    # A fresh module must not replay an unguarded previous motion sequence.
    # Imports are trusted Python dependencies, not a sandbox boundary.
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = [
                *node.args.defaults,
                *(v for v in node.args.kw_defaults if v is not None),
            ]
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            args += [v for v in (node.args.vararg, node.args.kwarg) if v is not None]
            safe_decorators = all(
                _dotted(d) in decorators
                or (
                    isinstance(d, ast.Call)
                    and _dotted(d.func) in skills
                    and all(_literal(a) for a in d.args)
                    and all(k.arg is not None and _literal(k.value) for k in d.keywords)
                )
                for d in node.decorator_list
            )
            if (
                all(_literal(v) for v in defaults)
                and safe_decorators
                and all(_annotation(a.annotation) for a in args)
                and _annotation(node.returns)
                and not getattr(node, "type_params", [])
            ):
                continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.If) and _main_guard(node):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                all(isinstance(t, ast.Name) for t in targets)
                and _literal(node.value)
                and (
                    not isinstance(node, ast.AnnAssign) or _annotation(node.annotation)
                )
            ):
                continue
        raise ValueError(
            f"Module initialization at line {node.lineno} would run before the selected entry. Move it into a function or an if __name__ == '__main__' block."
        )
    return entries


def execute_entry(source: str, filename: str, name: str) -> Any:
    """Execute a declared entry in fresh globals; no prior locals are reused."""
    if name not in {entry.name for entry in discover_entries(source)}:
        raise ValueError(f"No declared restart entry named {name}")
    namespace: dict[str, Any] = {"__name__": "__waldo_restart__", "__file__": filename}
    exec(compile(source, filename, "exec"), namespace)
    function = namespace[name]
    if not is_restart_entry(function):
        raise ValueError(f"{name} is no longer a declared restart entry")
    result = function()
    if inspect.isawaitable(result):

        async def finish():
            return await result

        return asyncio.run(finish())
    return result


@dataclass(frozen=True)
class RestartState:
    session_id: int
    seq: int
    controller_ns: int
    received_ns: int
    scene_epoch: int
    tool: str
    tool_variant: str
    tcp: tuple[float, ...]
    angles_deg: tuple[float, ...]
    speeds_rad_s: tuple[float, ...]
    homed: bool
    enabled: bool
    executing_index: int
    queue_empty: bool
    fault: bool
    freedrive: bool

    def require_ready(self) -> None:
        if not self.homed:
            raise ValueError("Reference the arm before restarting the program")
        if not self.enabled or self.fault:
            raise ValueError(
                "Resolve the controller fault or disabled state before restarting"
            )
        if not self.queue_empty or self.executing_index >= 0:
            raise ValueError(
                "Stop the previous controller motion and queue before restarting"
            )
        if (
            any(
                not math.isfinite(v)
                for v in (*self.angles_deg, *self.speeds_rad_s, *self.tcp)
            )
            or not self.angles_deg
            or len(self.speeds_rad_s) != len(self.angles_deg)
            or len(self.tcp) != 6
        ):
            raise ValueError("Controller state is unavailable")
        if any(abs(v) > 0.01 for v in self.speeds_rad_s):
            raise ValueError("Wait for the arm to stop before restarting")

    def require_same_setup(self, previous: RestartState) -> None:
        if (
            self.session_id,
            self.scene_epoch,
            self.tool,
            self.tool_variant,
            self.tcp,
        ) != (
            previous.session_id,
            previous.scene_epoch,
            previous.tool,
            previous.tool_variant,
            previous.tcp,
        ):
            raise ValueError(
                "Controller or setup changed. Review fresh state and check the physical setup again."
            )
        if len(self.angles_deg) != len(previous.angles_deg) or any(
            abs(a - b) > 1.0 for a, b in zip(self.angles_deg, previous.angles_deg)
        ):
            raise ValueError(
                "Arm position changed. Review fresh state and check the physical setup again."
            )


async def fresh_state(client: RobotClient, *, timeout: float = 3.0) -> RestartState:
    """Require advancing publications and read current queue/fault/TCP state."""
    async with asyncio.timeout(timeout):
        queue = await client.queue()
        if queue is None:
            # A lost readback is not an empty queue.
            raise ConnectionError(
                "The controller queue could not be read; review again"
            )
        error = await client.error()
        tcp = tuple(await client.tcp_transform())
        stream = client.stream_status()
        previous = None
        try:
            async for status in stream:
                identity = (
                    int(status.session_id),
                    int(status.seq),
                    int(status.mono_time_ns),
                )
                if (
                    previous is not None
                    and identity[0] > 0
                    and identity[0] == previous[0]
                    and identity[1] > previous[1]
                    and identity[2] > previous[2]
                ):
                    return RestartState(
                        *identity,
                        time.monotonic_ns(),
                        int(status.scene_epoch),
                        status.tool_status.key,
                        status.tool_status.variant_key,
                        tcp,
                        tuple(float(v) for v in status.angles),
                        tuple(float(v) for v in status.speeds),
                        bool(status.homed),
                        bool(status.enabled),
                        int(status.executing_index),
                        queue == [],
                        error is not None
                        or bool(status.collision_active)
                        or status.action_state == ActionState.ERROR,
                        bool(status.freedrive),
                    )
                previous = identity
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
    raise ConnectionError("Controller status stream ended")
