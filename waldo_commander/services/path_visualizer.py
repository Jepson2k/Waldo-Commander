"""
Path visualization service for robot program simulation.

Runs dry-run simulations in isolated subprocesses for safety and non-blocking
execution. Results are collected and applied to SimulationState in the main process.
"""

import asyncio
import builtins
import linecache
import logging
import os
import sys
import traceback
from dataclasses import asdict
from types import ModuleType
from collections.abc import Callable
from typing import Any, cast
import numpy as np

from nicegui import run

from waldoctl import LinearMotion

from waldo_commander.state import (
    simulation_state,
    PathSegment,
    ProgramTarget,
    ui_state,
    robot_state,
    editor_tabs_state,
)
from waldo_commander.common.logging_config import TRACE_ENABLED, TraceLogger

logger: TraceLogger = logging.getLogger(__name__)  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

# Configuration constants
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

    # Import the backend package to trigger pinokin/heavy imports.
    # Each backend is responsible for initializing its robot model on import.
    importlib.import_module(backend_package)
    from waldo_commander.services.path_preview_client import PathPreviewClient  # noqa: F401

    return True


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
        # Run warm-up in parallel across all workers
        # Each worker will import the backend once and stay warm
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
    # Local collectors (not shared with main process)
    local_segments: list[dict] = []
    local_targets: list[dict] = []
    local_tool_actions: list = []
    local_tool_selections: list = []
    # Track final state (updated by client on each motion)
    final_state: dict[str, Any] = {"joints_rad": None}
    truncated = False
    error_message: str | None = None

    import importlib

    from waldo_commander.services.path_preview_client import (
        PathPreviewClient,
        AsyncPathPreviewClient,
    )

    # Track created client instances so we can read final state after execution
    created_clients: list[PathPreviewClient] = []

    try:
        # Import the real backend and monkeypatch RobotClient/AsyncRobotClient
        # with preview clients. This runs in a subprocess so patching is safe.
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
                    initial_joints=initial_joints_rad,
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
                    initial_joints=initial_joints_rad,
                    dry_run_client_cls=_dr_cls,
                    tool_meta_registry=tool_meta_registry,
                )
                created_clients.append(self._sync_client)

        # Monkeypatch the real backend module (subprocess-only, safe)
        setattr(backend, "RobotClient", LocalPathPreviewClient)
        setattr(backend, "AsyncRobotClient", LocalAsyncPathPreviewClient)
        if hasattr(backend, "client"):
            setattr(backend.client, "RobotClient", LocalPathPreviewClient)
            setattr(backend.client, "AsyncRobotClient", LocalAsyncPathPreviewClient)

        # Create mock time module and insert into sys.modules
        # This ensures `import time` returns our mock instead of real time module
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
                # Only accumulate sleep after non-blocking moves —
                # after a blocking move the arm is already stationary.
                for client in created_clients:
                    if client._last_move_non_blocking:
                        client._pending_sleep += seconds

            @staticmethod
            def time():
                return 0.0

            @staticmethod
            def monotonic():
                return 0.0

            @staticmethod
            def perf_counter():
                return 0.0

            @staticmethod
            def perf_counter_ns():
                return 0

            @staticmethod
            def time_ns():
                return 0

        # Save original time module and replace with mock
        original_time_module = sys.modules.get("time")
        mock_time = MockTimeModule(original_time_module)
        sys.modules["time"] = mock_time

        # Prepare execution environment
        sim_globals = {
            "__name__": "__simulation__",
            "__file__": "simulation_script.py",
            "__builtins__": builtins.__dict__.copy(),
            "print": lambda *args, **kwargs: None,  # Suppress print
            "time": mock_time,  # Provide time module for scripts using time.sleep() without import
        }

        # Populate linecache so PathPreviewClient can read source lines
        # for literal-arg detection and line number extraction
        lines = program_text.splitlines(keepends=True)
        # Ensure lines end with newline for linecache compatibility
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        linecache.cache["simulation_script.py"] = (
            len(program_text),  # size
            None,  # mtime
            lines,  # lines
            "simulation_script.py",  # filename
        )

        try:
            # Compile the script with explicit filename so frame inspection works
            # This allows _get_caller_line_number() to find "simulation_script.py" frames
            code = compile(program_text, "simulation_script.py", "exec")

            # Execute the compiled code
            exec(code, sim_globals)

            # Check if there's a main() function and what type
            if "main" in sim_globals:
                main_func = sim_globals["main"]

                if asyncio.iscoroutinefunction(main_func):
                    # Async main - need to run the coroutine
                    # Try asyncio.run() first (subprocess context)
                    try:
                        coro = main_func()
                        asyncio.run(coro)
                    except RuntimeError as e:
                        if "cannot be called from a running event loop" in str(e):
                            # The coroutine from asyncio.run() was never awaited;
                            # close it explicitly to suppress the RuntimeWarning.
                            coro.close()
                            # We're in a running loop (fallback in-process mode)
                            # Create a new event loop in a thread
                            import concurrent.futures

                            def run_async_in_thread():
                                """Run the async main in a new event loop in this thread."""
                                return asyncio.run(main_func())

                            with concurrent.futures.ThreadPoolExecutor(
                                max_workers=1
                            ) as pool:
                                future = pool.submit(run_async_in_thread)
                                future.result(timeout=SIMULATION_TIMEOUT_S)
                        else:
                            raise

                elif callable(main_func):
                    # Sync main - just call it
                    cast(Callable[[], None], main_func)()

        except Exception as e:
            error_message = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

        finally:
            # Restore original time module
            if original_time_module is not None:
                sys.modules["time"] = original_time_module
            elif "time" in sys.modules and sys.modules["time"] is mock_time:
                del sys.modules["time"]

    except Exception as e:
        error_message = f"Simulation setup failed: {type(e).__name__}: {e}"

    # Flush pending blend buffers (handles scripts without context managers)
    for c in created_clients:
        c.close()

    # Extract final joints and accumulated errors from client instances
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

    # Enforce segment limit
    if len(local_segments) > max_segments:
        del local_segments[max_segments:]
        truncated = True

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


class PathVisualizer:
    """Visualizes robot path from program simulation."""

    def __init__(self):
        self._simulation_lock = asyncio.Lock()
        self._simulation_count = 0

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
            # Compare first/last point (same as scene fingerprint)
            if a.points and b.points:
                if a.points[0] != b.points[0] or a.points[-1] != b.points[-1]:
                    return False
        return True

    async def update_path_visualization(
        self, program_text: str, tab_id: str | None = None
    ) -> str | None:
        """
        Run the dry-run simulation for the given program text and update SimulationState.

        Executes simulation in an isolated subprocess for safety, then applies
        the results to the originating tab and (if still active) global simulation state.

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

            # Process pool is initialized by NiceGUI at startup and warmed by warm_process_pool()

            logger.info("Starting isolated path visualization (sim_id=%d)...", sim_id)

            if TRACE_ENABLED:
                segments_before = len(simulation_state.path_segments)
                targets_before = len(simulation_state.targets)
                logger.trace(
                    "PATHVIZ[%d]: Before simulation - segments=%d, targets=%d",
                    sim_id,
                    segments_before,
                    targets_before,
                )

            # Get current robot joint angles for initial position
            initial_joints_rad: np.ndarray | None = None
            if len(robot_state.angles) >= ui_state.active_robot.joints.count:
                initial_joints_rad = robot_state.angles.rad
                logger.debug(
                    "Using current robot joints as initial: %s deg",
                    robot_state.angles.deg,
                )

            # Get backend info from current robot
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

            try:
                # Run simulation in subprocess via NiceGUI's cpu_bound
                result = await asyncio.wait_for(
                    run.cpu_bound(
                        _run_simulation_isolated,
                        program_text,
                        initial_joints_rad,
                        MAX_PATH_SEGMENTS,
                        backend_pkg,
                        dr_cls,
                        tool_meta_registry or None,
                    ),
                    timeout=SIMULATION_TIMEOUT_S
                    + 2.0,  # Extra buffer for process overhead
                )
            except asyncio.TimeoutError:
                logger.error("Simulation subprocess timed out (sim_id=%d)", sim_id)
                return "Simulation timed out"
            except Exception as e:
                # Fallback to in-process execution when subprocess fails
                # (common in test environments where process pool is unavailable)
                logger.warning(
                    "Subprocess simulation failed (sim_id=%d): %s, using sync",
                    sim_id,
                    e,
                )
                try:
                    result = _run_simulation_isolated(
                        program_text,
                        initial_joints_rad,
                        MAX_PATH_SEGMENTS,
                        backend_pkg,
                        dr_cls,
                        tool_meta_registry or None,
                    )
                except Exception as e2:
                    logger.error("Sync simulation also failed: %s", e2)
                    return f"Simulation failed: {e2}"

            # Guard against None result (can happen during shutdown/test teardown)
            if result is None:
                logger.warning("Simulation returned None result (sim_id=%d)", sim_id)
                return "Simulation returned no result"

            # Handle errors
            if result.get("error"):
                logger.error(
                    "Simulation error (sim_id=%d): %s", sim_id, result["error"]
                )

            # Handle truncation warning
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

            # Store results in the originating tab (or active tab if no tab_id)
            target_tab = None
            if tab_id:
                target_tab = editor_tabs_state.find_tab_by_id(tab_id)
            if not target_tab:
                target_tab = editor_tabs_state.get_active_tab()

            if target_tab:
                new_segments = [PathSegment.from_dict(d) for d in result["segments"]]
                new_targets = [ProgramTarget.from_dict(d) for d in result["targets"]]
                new_tool_actions = result.get("tool_actions", [])
                new_tool_selections = result.get("tool_selections", [])

                # Always store final_joints_rad (used for position-change
                # detection even when segments are unchanged).
                target_tab.final_joints_rad = result.get("final_joints_rad")

                # Check if results match what's already stored — skip update
                # to avoid unnecessary scrub bar rebuilds and visual flash.
                # Don't skip when there's an error: the caller needs the error
                # string to apply diagnostics even if segments are the same.
                if self._segments_match(
                    target_tab.path_segments, new_segments
                ) and not result.get("error"):
                    logger.info(
                        "Simulation results unchanged (sim_id=%d), skipping update",
                        sim_id,
                    )
                    return UNCHANGED

                # Store simulation results in the tab
                target_tab.path_segments = new_segments
                target_tab.targets = new_targets
                target_tab.tool_actions = new_tool_actions
                target_tab.tool_selections = new_tool_selections

                # Only update global simulation_state if this tab is still active
                if target_tab.id == editor_tabs_state.active_tab_id:
                    simulation_state.path_segments = list(target_tab.path_segments)
                    simulation_state.targets = list(target_tab.targets)
                    simulation_state.tool_actions = list(target_tab.tool_actions)
                    simulation_state.tool_selections = list(target_tab.tool_selections)
                    simulation_state.total_steps = len(target_tab.path_segments)
                else:
                    logger.debug(
                        "Simulation for tab %s complete, but tab no longer active - "
                        "skipping global state update",
                        tab_id,
                    )

            # Trigger scene update via event-driven notification (diff rendering
            # handles add/remove/change without needing invalidate_paths)
            simulation_state.notify_changed()

            # Return error message if any
            return result.get("error")


# Singleton instance
path_visualizer = PathVisualizer()
