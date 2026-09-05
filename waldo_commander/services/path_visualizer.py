"""
Path visualization service for robot program simulation.

Runs dry-run simulations in isolated subprocesses for safety and non-blocking
execution. Results are collected and applied to the originating program's
dry-run in the main process.
"""

import asyncio
import builtins
import linecache
import logging
import os
import sys
import threading
import traceback
from dataclasses import asdict
from types import ModuleType
from collections.abc import Callable
from typing import Any, cast
import numpy as np

from nicegui import run
from nicegui import app as ng_app

import waldoctl
from waldoctl import LinearMotion

from waldo_commander.state import (
    robot_state,
    simulation_state,
    PathSegment,
    ProgramTarget,
    ui_state,
)
from waldo_commander.common.logging_config import TRACE_ENABLED, TraceLogger
from waldo_commander.common.theme import SceneColors

logger: TraceLogger = logging.getLogger(__name__)  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

MAX_PATH_SEGMENTS = 10000
SIMULATION_TIMEOUT_S = 5.0

# Sentinel returned by update_path_visualization when results are unchanged
UNCHANGED = "__unchanged__"


def _warm_worker(backend_package: str = "parol6") -> bool:
    """Import heavy modules in subprocess worker. Called once per worker at startup."""
    import importlib
    import signal

    # Ignore SIGINT in worker - main process handles shutdown
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    # Triggers pinokin/heavy imports; each backend initializes its robot model on import.
    importlib.import_module(backend_package)
    from waldo_commander.services.path_preview_client import PathPreviewClient  # noqa: F401

    return True


def _mark_colliding_segments(
    robot,
    segment_dicts: list[dict],
    tool_selections: list,
    shape_changes: list,
    shapes_wire: list[tuple] | None,
    initial_tool: tuple[str, str] | None,
) -> None:
    """Recolor segment dicts whose joint trajectory collides (self/tool/shape).

    Runs in the dry-run subprocess against its own checker, replaying BOTH
    boundary streams the dry run recorded — ``select_tool`` and ``set_shapes``
    — so each segment is checked with the tool attached and the world active
    at its point in the program (the dry run itself only validates IK). Each
    hit records its first colliding waypoint in ``collision_step``.

    The checker's tool and program world are restored on exit — the rare
    in-process fallback shares the live checker.
    """
    if not robot.has_collision_checking:
        return
    from waldoctl import shape_from_wire

    # Unconditional — including the EMPTY set: a reused pool worker keeps its
    # process-global checker between runs, so a cleared world must clear it.
    submit_world = [shape_from_wire(*t) for t in shapes_wire or []]
    robot.apply_shapes(submit_world)
    tool_key, variant = initial_tool or ("NONE", "")
    try:
        robot.set_active_tool(tool_key, variant_key=variant or None)
        # A boundary recorded at segment_index i applies to segments after i.
        # Recorded order IS chronological (indexes are non-decreasing) — a sort
        # would reorder same-index back-to-back entries and replay the wrong
        # state.
        tool_bounds = [
            (ts.segment_index, ts.tool_key, ts.variant_key) for ts in tool_selections
        ]
        shape_bounds = [(sc.segment_index, sc.shapes) for sc in shape_changes]
        ti = si = 0
        for idx, d in enumerate(segment_dicts):
            while ti < len(tool_bounds) and tool_bounds[ti][0] < idx:
                _, b_tool, b_variant = tool_bounds[ti]
                robot.set_active_tool(b_tool, variant_key=b_variant or None)
                ti += 1
            while si < len(shape_bounds) and shape_bounds[si][0] < idx:
                robot.apply_shapes(list(shape_bounds[si][1]))
                si += 1
            jt = d.get("joint_trajectory")
            if not jt:
                continue
            hit = robot.check_trajectory(np.asarray(jt, dtype=np.float64))
            if hit >= 0:
                d["collision_step"] = int(hit)
                d["color"] = SceneColors.COLLISION_HEX
    finally:
        robot.set_active_tool(tool_key, variant_key=variant or None)
        robot.apply_shapes(submit_world)


def _is_test_environment() -> bool:
    """Detect if running under pytest or similar test environment."""
    return (
        "pytest" in sys.modules
        or "__main__" not in sys.modules
        or os.environ.get("PYTEST_CURRENT_TEST") is not None
    )


async def warm_process_pool(backend_package: str = "parol6") -> None:
    """Pre-warm all process pool workers by importing heavy modules.

    This should be called once at app startup (after NiceGUI has initialized
    the process pool). Each worker process will import the backend package
    once, and subsequent simulations will be fast since workers are reused.

    Skipped in test environments where multiprocessing spawn doesn't work properly.

    Args:
        backend_package: Backend package to import in workers (e.g. "parol6")
    """
    if _is_test_environment():
        logger.debug("Skipping process pool warming in test environment")
        return

    # ProcessPoolExecutor uses cpu_count() workers by default
    worker_count = os.cpu_count() or 4
    logger.info(
        "Warming %d process pool workers (importing %s)...",
        worker_count,
        backend_package,
    )

    try:
        # One import per worker, in parallel, so each stays warm for later sims.
        futures = [
            run.cpu_bound(_warm_worker, backend_package) for _ in range(worker_count)
        ]
        await asyncio.gather(*futures)
        logger.info("Process pool workers warmed successfully")
    except Exception as e:
        logger.warning("Failed to warm process pool workers: %s", e)


def _run_simulation_isolated(
    program_text: str,
    initial_joints_rad: np.ndarray | None = None,
    max_segments: int = MAX_PATH_SEGMENTS,
    backend_package: str = "parol6",
    dry_run_client_cls: type | None = None,
    tool_meta_registry: dict[str, dict] | None = None,
    shapes_wire: list[tuple] | None = None,
    initial_tool: tuple[str, str] | None = None,
    initial_homed: bool = True,
) -> dict[str, Any]:
    """
    Run dry-run simulation in isolated subprocess.

    This function is designed to be called via run.cpu_bound() for process
    isolation. It returns serializable results (dicts) rather than modifying
    global state.

    The simulation starts with no tool attached. The script must call
    select_tool() explicitly to configure the correct tool and variant.

    Args:
        program_text: The Python program to simulate
        initial_joints_rad: Initial joint angles in radians (robot's current position)
        max_segments: Maximum path segments to collect (prevents memory exhaustion)
        backend_package: Backend package name for module shimming
        dry_run_client_cls: Concrete DryRunRobotClient class for path preview
        tool_meta_registry: Mapping of tool_key → {motions, variants, activation_type}

    Returns:
        Dict with keys:
        - segments: List of path segment dicts
        - targets: List of program target dicts
        - truncated: Whether results were truncated
        - error: Error message if simulation failed, else None
        - total_steps: Number of segments generated
    """
    # Collectors local to this subprocess, not shared with the main process.
    local_segments: list[dict] = []
    local_targets: list[dict] = []
    local_tool_actions: list = []
    local_tool_selections: list = []
    local_shape_changes: list = []
    # Updated by the client on each motion.
    final_state: dict[str, Any] = {"joints_rad": None}
    truncated = False
    error_message: str | None = None

    import importlib

    from waldo_commander.services.path_preview_client import (
        PathPreviewClient,
        AsyncPathPreviewClient,
    )

    # Lets us read final state after execution.
    created_clients: list[PathPreviewClient] = []

    try:
        # Monkeypatch RobotClient/AsyncRobotClient with preview clients; safe
        # because this runs in a subprocess.
        backend = importlib.import_module(backend_package)
        assert dry_run_client_cls is not None

        _dr_cls: type = dry_run_client_cls

        class LocalPathPreviewClient(PathPreviewClient):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(
                    segment_collector=local_segments,
                    target_collector=local_targets,
                    tool_action_collector=local_tool_actions,
                    tool_selection_collector=local_tool_selections,
                    shape_change_collector=local_shape_changes,
                    initial_joints=initial_joints_rad,
                    initial_homed=initial_homed,
                    dry_run_client_cls=_dr_cls,
                    tool_meta_registry=tool_meta_registry,
                )
                created_clients.append(self)

        class LocalAsyncPathPreviewClient(AsyncPathPreviewClient):
            def __init__(self, *args: Any, **kwargs: Any):
                self._sync_client = PathPreviewClient(
                    segment_collector=local_segments,
                    target_collector=local_targets,
                    tool_action_collector=local_tool_actions,
                    tool_selection_collector=local_tool_selections,
                    shape_change_collector=local_shape_changes,
                    initial_joints=initial_joints_rad,
                    initial_homed=initial_homed,
                    dry_run_client_cls=_dr_cls,
                    tool_meta_registry=tool_meta_registry,
                )
                created_clients.append(self._sync_client)

        setattr(backend, "RobotClient", LocalPathPreviewClient)
        setattr(backend, "AsyncRobotClient", LocalAsyncPathPreviewClient)
        if hasattr(backend, "client"):
            setattr(backend.client, "RobotClient", LocalPathPreviewClient)
            setattr(backend.client, "AsyncRobotClient", LocalAsyncPathPreviewClient)

        # Reset this worker's program-layer world to the submit-time truth
        # BEFORE the script runs: a reused pool worker's process-global checker
        # otherwise carries a previous run's shapes into this run's planning
        # guard. Empty included. Installation shapes come from robot config at
        # backend import and are untouched.
        from waldo_commander.profiles import get_robot
        from waldoctl import shape_from_wire

        _preview_robot = get_robot(backend_package)
        if _preview_robot.has_collision_checking:
            _preview_robot.apply_shapes(
                [shape_from_wire(*t) for t in shapes_wire or []]
            )

        # Inserted into sys.modules so `import time` returns this mock. The
        # mock behavior is scoped to the simulating thread: in the thread
        # fallback the app's event loop keeps running concurrently and must
        # keep seeing real clocks (in a pool worker there is only one thread,
        # so the scoping is a no-op).
        sim_thread_id = threading.get_ident()

        class MockTimeModule(ModuleType):
            """Mock time module with no-op sleep for simulation."""

            def __init__(self, real_time_module):
                super().__init__("time")
                self.__file__ = "<mock_time>"
                self.__package__ = ""
                self._real_time = real_time_module

            def __getattr__(self, name):
                return getattr(self._real_time, name)

            def sleep(self, seconds):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.sleep(seconds)
                # Only accumulate sleep after non-blocking moves —
                # after a blocking move the arm is already stationary.
                for client in created_clients:
                    if client._last_move_non_blocking:
                        client._pending_sleep += seconds

            def time(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.time()
                return 0.0

            def monotonic(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.monotonic()
                return 0.0

            def perf_counter(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.perf_counter()
                return 0.0

            def perf_counter_ns(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.perf_counter_ns()
                return 0

            def time_ns(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.time_ns()
                return 0

        original_time_module = sys.modules.get("time")
        mock_time = MockTimeModule(original_time_module)
        sys.modules["time"] = mock_time

        sim_globals = {
            "__name__": "__simulation__",
            "__file__": "simulation_script.py",
            "__builtins__": builtins.__dict__.copy(),
            "print": lambda *args, **kwargs: None,
            "time": mock_time,  # Scripts may use time.sleep() without importing it.
        }

        # Populate linecache so PathPreviewClient can read source lines
        # for literal-arg detection and line number extraction.
        lines = program_text.splitlines(keepends=True)
        # linecache requires a trailing newline on every line.
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        linecache.cache["simulation_script.py"] = (
            len(program_text),
            None,  # mtime
            lines,
            "simulation_script.py",
        )

        try:
            # Explicit filename so _get_caller_line_number() can find
            # "simulation_script.py" frames during inspection.
            code = compile(program_text, "simulation_script.py", "exec")

            exec(code, sim_globals)

            if "main" in sim_globals:
                main_func = sim_globals["main"]

                if asyncio.iscoroutinefunction(main_func):
                    # asyncio.run() works in the normal subprocess context.
                    try:
                        coro = main_func()
                        asyncio.run(coro)
                    except RuntimeError as e:
                        if "cannot be called from a running event loop" in str(e):
                            # The coroutine from asyncio.run() was never awaited;
                            # close it explicitly to suppress the RuntimeWarning.
                            coro.close()
                            # Fallback in-process mode: already inside a running
                            # loop, so spin up a fresh loop in a thread.
                            import concurrent.futures

                            def run_async_in_thread():
                                return asyncio.run(main_func())

                            with concurrent.futures.ThreadPoolExecutor(
                                max_workers=1
                            ) as pool:
                                future = pool.submit(run_async_in_thread)
                                future.result(timeout=SIMULATION_TIMEOUT_S)
                        else:
                            raise

                elif callable(main_func):
                    cast(Callable[[], None], main_func)()

        except Exception as e:
            error_message = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

        finally:
            if original_time_module is not None:
                sys.modules["time"] = original_time_module
            elif "time" in sys.modules and sys.modules["time"] is mock_time:
                del sys.modules["time"]

    except Exception as e:
        error_message = f"Simulation setup failed: {type(e).__name__}: {e}"

    # Flush pending blend buffers, covering scripts without context managers.
    for c in created_clients:
        c.close()

    if created_clients:
        last_client = created_clients[-1]
        if last_client.last_joints_rad is not None:
            final_state["joints_rad"] = last_client.last_joints_rad
        for c in created_clients:
            if c.accumulated_errors:
                errors_text = "\n".join(c.accumulated_errors)
                if error_message:
                    error_message += "\n" + errors_text
                else:
                    error_message = errors_text

    if len(local_segments) > max_segments:
        del local_segments[max_segments:]
        truncated = True

    # Collision marking runs here (normally a subprocess) so 1000s of C++
    # checks never block the UI event loop and mid-script tool selections are
    # honored. A marking failure must not discard an otherwise-good dry run.
    try:
        from waldo_commander.profiles import get_robot

        _mark_colliding_segments(
            get_robot(backend_package),
            local_segments,
            local_tool_selections,
            local_shape_changes,
            shapes_wire,
            initial_tool,
        )
    except Exception as e:
        logger.warning("Preview collision marking failed: %s", e)

    return {
        "segments": local_segments,
        "targets": local_targets,
        "tool_actions": local_tool_actions,
        "tool_selections": local_tool_selections,
        "truncated": truncated,
        "error": error_message,
        "total_steps": len(local_segments),
        "final_joints_rad": final_state.get("joints_rad"),
    }


def _run_simulation_packed(args: tuple) -> dict[str, Any]:
    """Single-argument adapter for run.cpu_bound, whose ParamSpec cannot type
    a heterogeneous *args unpack; packing also lets the pool call and the
    in-process fallback share one argument list."""
    return _run_simulation_isolated(*args)


def _track_signature(seg: PathSegment) -> tuple:
    """What a segment's object tracks look like: per object, how many rows,
    where it lands, and whether that is physics or a guess."""
    tracks = seg.object_tracks or ()
    return tuple(
        (
            t["name"],
            len(t["poses"]),
            tuple(t["poses"][-1]) if t["poses"] else (),
            bool(t.get("carried")),
            bool(t.get("physics", True)),
        )
        for t in tracks
    )


class PathVisualizer:
    """Visualizes robot path from program simulation."""

    def __init__(self):
        self._simulation_lock = asyncio.Lock()
        self._simulation_count = 0

    def reset_for_test(self) -> None:
        """Rebuild loop-bound state so the next test's event loop starts clean.

        ``asyncio.Lock`` binds to a loop the first time it is acquired while
        already held, and stays bound. A simulation still in flight when its
        test ends leaves this lock acquired against a loop that is about to
        close, so the next test's simulation takes the contended path and
        raises ``bound to a different event loop``.
        """
        type(self).__init__(self)

    @staticmethod
    def _segments_match(old: list[PathSegment], new: list[PathSegment]) -> bool:
        """Fast check whether two segment lists are visually identical."""
        if len(old) != len(new):
            return False
        for a, b in zip(old, new):
            if (
                len(a.points) != len(b.points)
                or a.color != b.color
                or a.is_valid != b.is_valid
                or a.line_number != b.line_number
            ):
                return False
            # First/last point, matching the scene fingerprint.
            if a.points and b.points:
                if a.points[0] != b.points[0] or a.points[-1] != b.points[-1]:
                    return False
            # Where the objects end up is part of the picture too.
            if _track_signature(a) != _track_signature(b):
                return False
        return True

    async def update_path_visualization(
        self, program_text: str, tab_id: str | None = None
    ) -> str | None:
        """
        Run the dry-run simulation for the given program text and update the
        originating program's dry-run.

        Executes simulation in an isolated subprocess for safety, then applies
        the results to the originating tab's ``dry_run``.

        Args:
            program_text: The Python program to simulate
            tab_id: Optional tab ID that triggered this simulation. Results will be
                stored in this tab. If None, uses active tab.

        Returns:
            Error message if simulation failed, None otherwise.
        """
        async with self._simulation_lock:
            self._simulation_count += 1
            sim_id = self._simulation_count

            # Process pool is initialized by NiceGUI at startup and warmed by warm_process_pool().
            logger.info("Starting isolated path visualization (sim_id=%d)...", sim_id)

            if TRACE_ENABLED:
                _trace_tab = waldoctl.commander.programs.active
                segments_before = (
                    len(_trace_tab.dry_run.path_segments)
                    if _trace_tab is not None
                    else 0
                )
                targets_before = (
                    len(_trace_tab.dry_run.targets) if _trace_tab is not None else 0
                )
                logger.trace(
                    "PATHVIZ[%d]: Before simulation - segments=%d, targets=%d",
                    sim_id,
                    segments_before,
                    targets_before,
                )

            # Current robot joint angles seed the simulation's initial position.
            initial_joints_rad: np.ndarray | None = None
            if (
                len(waldoctl.commander.status.joints.angles)
                >= ui_state.active_robot.joints.count
            ):
                initial_joints_rad = waldoctl.commander.status.joints.angles.rad
                logger.debug(
                    "Using current robot joints as initial: %s deg",
                    waldoctl.commander.status.joints.angles.deg,
                )

            robot = ui_state.active_robot
            backend_pkg = robot.backend_package
            dr_instance = robot.create_dry_run_client()
            dr_cls = type(dr_instance) if dr_instance is not None else None
            if dr_cls is None:
                logger.warning(
                    "Backend %s does not support dry-run simulation", backend_pkg
                )
                simulation_state.notify_changed()
                return None

            # Build serializable tool metadata registry for all tools.
            # Scripts can call select_tool() to switch tools mid-program, so we
            # need metadata for every tool — not just the currently active one.
            # Each entry includes base motions + per-variant motions.
            tool_meta_registry: dict[str, dict] = {}

            def _serialize_motions(motion_list):
                return [
                    {"type": "linear", **asdict(m)}
                    if isinstance(m, LinearMotion)
                    else {"type": "rotary", **asdict(m)}
                    for m in motion_list
                ]

            for spec in robot.tools.available:
                if spec.key == "NONE":
                    continue
                try:
                    base_motions = (
                        _serialize_motions(spec.motions) if spec.motions else []
                    )
                    variants_dict: dict[str, dict] = {}
                    for v in spec.variants:
                        if v.motions:
                            variants_dict[v.key] = {
                                "motions": _serialize_motions(v.motions),
                            }
                    if not base_motions and not variants_dict:
                        continue
                    tool_meta_registry[spec.key] = {
                        "motions": base_motions,
                        "variants": variants_dict,
                        "activation_type": spec.activation_type.value,
                    }
                except (KeyError, AttributeError):
                    pass

            # Collision-marking inputs: the live shapes (wire form crosses the
            # process boundary) and the live tool as the checker's starting
            # state — matching what execution-time guards would use.
            scene_handle = waldoctl.commander.scene
            shapes_wire = (
                [s.to_wire() for s in scene_handle.enforced_locally]
                if scene_handle is not None
                else []
            )
            live_tool_key = waldoctl.commander.status.tool.key or "NONE"
            initial_tool = (
                live_tool_key,
                ng_app.storage.general.get(f"tool_variant_{live_tool_key}", "") or "",
            )
            # Live homed state seeds the preview so it mirrors the controller's
            # planned-motion gate: an unhomed robot's preview refuses planned
            # moves until the script homes.
            initial_homed = robot_state.homed

            sim_args = (
                program_text,
                initial_joints_rad,
                MAX_PATH_SEGMENTS,
                backend_pkg,
                dr_cls,
                tool_meta_registry or None,
                shapes_wire,
                initial_tool,
                initial_homed,
            )
            # Tests always simulate in-process: the pool is rebuilt per test
            # with warm-up disabled, so a cold spawn worker could not meet the
            # timeout even when submission succeeds. The pool remains a
            # best-effort optimization elsewhere, with the same in-process
            # path as fallback.
            use_pool = run.process_pool is not None and not _is_test_environment()
            if use_pool:
                try:
                    result = await asyncio.wait_for(
                        run.cpu_bound(_run_simulation_packed, sim_args),
                        timeout=SIMULATION_TIMEOUT_S
                        + 2.0,  # Extra buffer for process overhead
                    )
                except asyncio.TimeoutError:
                    logger.error("Simulation subprocess timed out (sim_id=%d)", sim_id)
                    return "Simulation timed out"
                except Exception as e:
                    logger.warning(
                        "Subprocess simulation failed (sim_id=%d): %s, using thread",
                        sim_id,
                        e,
                    )
                    use_pool = False
            if not use_pool:
                # A worker thread (not inline) so the event loop keeps running
                # and the script's own asyncio.run() has no running loop in its
                # thread — async programs can't be simulated inline at all.
                try:
                    result = await asyncio.to_thread(_run_simulation_packed, sim_args)
                except Exception as e2:
                    logger.error("Thread simulation failed: %s", e2)
                    return f"Simulation failed: {e2}"

            # A None result can happen during shutdown/test teardown.
            if result is None:
                logger.warning("Simulation returned None result (sim_id=%d)", sim_id)
                return "Simulation returned no result"

            if result.get("error"):
                logger.error(
                    "Simulation error (sim_id=%d): %s", sim_id, result["error"]
                )

            if result.get("truncated"):
                logger.warning(
                    "Simulation truncated to %d segments (sim_id=%d)",
                    MAX_PATH_SEGMENTS,
                    sim_id,
                )

            logger.info(
                "Simulation complete (sim_id=%d). Generated %d path segments.",
                sim_id,
                len(result["segments"]),
            )

            # Store results in the originating tab, falling back to the active tab.
            target_tab = None
            if tab_id:
                target_tab = waldoctl.commander.programs.get(tab_id)
            if not target_tab:
                target_tab = waldoctl.commander.programs.active

            if target_tab:
                new_segments = [PathSegment.from_dict(d) for d in result["segments"]]
                new_targets = [ProgramTarget.from_dict(d) for d in result["targets"]]
                new_tool_actions = result.get("tool_actions", [])
                new_tool_selections = result.get("tool_selections", [])

                # Always store final_joints_rad (used for position-change
                # detection even when segments are unchanged).
                target_tab.dry_run.final_joints_rad = result.get("final_joints_rad")

                # Check if results match what's already stored — skip update
                # to avoid unnecessary scrub bar rebuilds and visual flash.
                # Don't skip when there's an error: the caller needs the error
                # string to apply diagnostics even if segments are the same.
                if self._segments_match(
                    target_tab.dry_run.path_segments, new_segments
                ) and not result.get("error"):
                    logger.info(
                        "Simulation results unchanged (sim_id=%d), skipping update",
                        sim_id,
                    )
                    return UNCHANGED

                target_tab.dry_run.path_segments = new_segments
                target_tab.dry_run.targets = new_targets
                target_tab.dry_run.tool_actions = new_tool_actions
                target_tab.dry_run.tool_selections = new_tool_selections
                target_tab.dry_run.total_steps = len(new_segments)

                # Dry-run results live on the target tab; readers go through
                # ``commander.programs.active.dry_run`` so the WC-side change
                # notification below fires regardless of which tab is active.
                if target_tab.id != waldoctl.commander.programs.active_id:
                    logger.debug(
                        "Simulation for tab %s complete, but tab no longer active - "
                        "results stored on its dry-run, will render on next switch",
                        tab_id,
                    )

            # Diff rendering handles add/remove/change without invalidate_paths.
            simulation_state.notify_changed()

            return result.get("error")


path_visualizer = PathVisualizer()
