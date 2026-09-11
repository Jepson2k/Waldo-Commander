"""Reusable local cases run through the isolated program and physics preview.

Case files contain trusted Python, just like editor programs. Loading a case
only reads data; running it starts a disposable preview worker.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from importlib import import_module
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class SimulationCase:
    name: str
    program: str
    initial_joints_deg: tuple[float, ...]
    schema: int = 1
    backend: str = "par6"
    max_seconds: float = 30.0
    wall_timeout_s: float = 65.0
    scenario: dict[str, Any] = field(default_factory=dict)
    world: dict[str, Any] | None = None
    initial_tool: tuple[str, str] | None = None
    initial_homed: bool = True
    expected_stop: str = "completed"
    expected_error_code: int | None = None
    assumptions: str = ""

    def __post_init__(self) -> None:
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError("Unsupported simulation case schema")
        for name in ("name", "program", "backend"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        if not isinstance(self.assumptions, str):
            raise ValueError("assumptions must be text")
        if (
            not isinstance(self.initial_joints_deg, (list, tuple))
            or len(self.initial_joints_deg) != 6
        ):
            raise ValueError("initial_joints_deg must contain six finite angles")
        values = (*self.initial_joints_deg, self.max_seconds, self.wall_timeout_s)
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError("Angles and time limits must be finite numbers")
        if not 0 < self.max_seconds <= 3600 or not 0 < self.wall_timeout_s <= 600:
            raise ValueError(
                "max_seconds must be in (0, 3600]; wall_timeout_s in (0, 600]"
            )
        if type(self.initial_homed) is not bool or not isinstance(self.scenario, dict):
            raise ValueError("initial_homed must be boolean and scenario an object")
        if self.initial_tool is not None and (
            not isinstance(self.initial_tool, (list, tuple))
            or len(self.initial_tool) != 2
            or any(not isinstance(v, str) for v in self.initial_tool)
            or not self.initial_tool[0]
        ):
            # The variant may be empty, which is how a tool without variants is
            # selected -- the shape the preview itself seeds with. Demanding one
            # meant no case could select its starting tool at all, and inventing
            # a variant to satisfy the check is refused by the runtime.
            raise ValueError(
                "initial_tool must be a tool key and a variant key, the variant "
                "empty for a tool without variants"
            )
        if self.expected_stop not in ("completed", "failed", "budget_exhausted"):
            raise ValueError(
                "expected_stop must be completed, failed, or budget_exhausted"
            )
        if self.expected_stop == "failed":
            if (
                type(self.expected_error_code) is not int
                or self.expected_error_code < 0
            ):
                raise ValueError("A failed case requires an expected_error_code")
        elif self.expected_error_code is not None:
            raise ValueError("expected_error_code only applies to a failed case")
        if self.world is not None:
            from waldoctl.world import world_from_dict

            world = world_from_dict(self.world)
            if world.installation:
                raise ValueError("Installation geometry comes from the backend model")
            if any(shape.attachment is not None for shape in world.program):
                raise ValueError(
                    "Declare attachments in the case program using its fresh preview context"
                )


def load_case(path: str | Path) -> SimulationCase:
    """Read and validate a case without importing its backend or running Python."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("A simulation case must be a JSON object")
    return SimulationCase(**data)


#: Said on every report: the numbers are a model's, not a measurement's.
_MODEL_NOTE = (
    "Simulation output. Perturbations, powered support, friction, and supply "
    "decay are assumed inputs; this is not a measured hardware or capacitor "
    "response."
)


def _run_case_worker(args: tuple[dict[str, Any]]) -> dict[str, Any]:
    from waldoctl.world import world_from_dict

    from waldo_commander.profiles import get_robot
    from waldo_commander.services.path_visualizer import (
        _run_simulation_isolated,
        _tool_metadata,
    )

    case = SimulationCase(**args[0])
    robot = get_robot(case.backend)
    # Case replay uses the installed model; a nearby live daemon must not
    # silently substitute a different configuration for the same case.
    if case.backend != "par6":
        raise ValueError(
            f"Backend {case.backend!r} does not support simulation scenarios"
        )
    client = import_module("par6.client.dry_run_client").DryRunRobotClient()
    if client is None or "simulation.scenarios" not in client.skill_capabilities:
        raise ValueError(
            f"Backend {case.backend!r} does not support simulation scenarios"
        )
    world = world_from_dict(case.world) if case.world is not None else None
    result = _run_simulation_isolated(
        case.program,
        initial_joints_rad=np.deg2rad(case.initial_joints_deg),
        backend_package=robot.backend_package,
        dry_run_client_cls=type(client),
        tool_meta_registry=_tool_metadata(robot),
        shapes_wire=[shape.to_wire() for shape in world.program] if world else None,
        initial_tool=case.initial_tool,
        initial_homed=case.initial_homed,
        simulate_seconds=case.max_seconds,
        scenario=case.scenario,
    )
    ticks = result["ticks"]
    error = result["error"] or result["physics_error"]
    errors = (
        [
            {
                "command": block.command,
                "code": getattr(block.error, "code", None),
                "detail": str(block.error),
            }
            for block in ticks.blocks
            if block.error is not None
        ]
        if ticks is not None
        else []
    )
    stop = "error" if error or result["truncated"] or ticks is None else ticks.stop
    passed = stop == case.expected_stop and (
        case.expected_error_code is None
        or any(e["code"] == case.expected_error_code for e in errors)
    )
    model_root = Path(str(files(case.backend))) / "_data"
    model_digest = hashlib.sha256()
    for path in sorted(model_root.rglob("*")):
        if path.is_file():
            model_digest.update(
                path.relative_to(model_root).as_posix().encode() + b"\0"
            )
            model_digest.update(hashlib.sha256(path.read_bytes()).digest())
    return {
        "name": case.name,
        "passed": passed,
        "stop": stop,
        "error": error,
        "command_errors": errors,
        "expected_stop": case.expected_stop,
        "expected_error_code": case.expected_error_code,
        "backend": case.backend,
        "backend_version": version(case.backend),
        "commander_version": version("waldo-commander"),
        "program_sha256": hashlib.sha256(case.program.encode()).hexdigest(),
        "model_sha256": model_digest.hexdigest(),
        "case_sha256": hashlib.sha256(
            json.dumps(asdict(case), sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
        "scenario": case.scenario,
        "assumptions": case.assumptions,
        "model_note": _MODEL_NOTE,
        "duration_s": ticks.duration_s if ticks is not None else 0.0,
        "rows": ticks.rows if ticks is not None else 0,
        "digest": ticks.digest.hex() if ticks is not None else "",
        "final_joints_deg": np.rad2deg(ticks.joints_rad[-1]).tolist()
        if ticks is not None and ticks.rows
        else None,
    }


def _unrun_report(case: SimulationCase, stop: str, error: str) -> dict[str, Any]:
    """A report for a case that never produced ticks, with every field the
    documented shape carries.

    A consumer reading anything beyond ``passed`` -- the duration, the digest,
    the versions the run was made with -- used to get a KeyError for exactly
    the cases worth investigating.
    """
    return {
        "name": case.name,
        "passed": False,
        "stop": stop,
        "error": error,
        "command_errors": [],
        "expected_stop": case.expected_stop,
        "expected_error_code": case.expected_error_code,
        "backend": case.backend,
        "backend_version": version(case.backend),
        "commander_version": version("waldo-commander"),
        "program_sha256": hashlib.sha256(case.program.encode()).hexdigest(),
        "model_sha256": "",
        "case_sha256": hashlib.sha256(
            json.dumps(asdict(case), sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
        "scenario": case.scenario,
        "assumptions": case.assumptions,
        "model_note": _MODEL_NOTE,
        "duration_s": 0.0,
        "rows": 0,
        "digest": "",
        "final_joints_deg": None,
    }


async def run_case(case: SimulationCase) -> dict[str, Any]:
    """Run one case in a disposable process with a wall-clock deadline."""
    from waldo_commander.services.path_visualizer import _PhysicsPool

    pool = _PhysicsPool()
    try:
        return await asyncio.wait_for(
            pool.run(_run_case_worker, (asdict(case),)), timeout=case.wall_timeout_s
        )
    except TimeoutError:
        return _unrun_report(
            case,
            "wall_timeout",
            "Preview worker exceeded its wall-clock deadline",
        )
    except Exception as exc:
        return _unrun_report(case, "error", f"{type(exc).__name__}: {exc}")
    finally:
        pool.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run local simulation cases sequentially"
    )
    parser.add_argument("cases", type=Path, nargs="+")
    parser.add_argument(
        "--output", type=Path, help="Write the JSON report to this file"
    )
    args = parser.parse_args()

    async def run_all() -> list[dict[str, Any]]:
        return [await run_case(load_case(path)) for path in args.cases]

    reports = asyncio.run(run_all())
    text = json.dumps(reports, indent=2, allow_nan=False)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if all(report["passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
