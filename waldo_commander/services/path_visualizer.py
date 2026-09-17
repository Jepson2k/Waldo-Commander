"""
Path visualization service for robot program simulation.

Runs dry-run simulations in isolated subprocesses for safety and non-blocking
execution. A worker runs the program against the backend's dry-run client
and brings back the program's records — the *commanded* one, what every
command tells the arm to do, and on the predicted pass what the arm would
do — with the host's notes on each command. The main process derives what
the scene draws from them and applies it to the originating program.

The two passes are paired by the program's revision: the frontend bumps it
on every change that re-plans, each pass carries the revision it was
launched for, and a predicted record lands only against the commanded one
it answers, so a slow pass never draws over a newer plan.
"""

import asyncio
import builtins
import inspect
import linecache
import logging
import multiprocessing
import os
import pickle
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from types import ModuleType
from typing import Any
import numpy as np

from nicegui import run
from nicegui import app as ng_app

import waldoctl
from waldoctl import CommandNote, LinearMotion, TickIndex
from waldoctl.skills import UnresolvedPreview

from waldo_commander.services.preview_segments import (
    index_boundaries,
    segments_from_record,
    tool_actions_from_record,
)
from waldo_commander.state import (
    robot_state,
    simulation_state,
    ProgramTarget,
    ui_state,
)
from waldo_commander.common.logging_config import TRACE_ENABLED, TraceLogger

logger: TraceLogger = logging.getLogger(__name__)  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

SIMULATION_TIMEOUT_S = 5.0

#: Simulated seconds a physics pass may cover before it gives up and
#: reports what it has. Ten minutes of robot time is roughly ten seconds
#: of computing; a program that never terminates would otherwise never
#: come back.
MAX_SIMULATED_SECONDS = 600.0

#: Wall-clock ceiling on a physics pass. `MAX_SIMULATED_SECONDS` bounds
#: only the simulation, which runs after the script returns — a program
#: that never terminates reaches neither cap without this.
PHYSICS_TIMEOUT_S = SIMULATION_TIMEOUT_S + 60.0

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


def _mark_colliding_commands(
    robot,
    record: TickIndex,
    tool_selections: list,
    shape_changes: list,
    shapes_wire: list[tuple] | None,
    initial_tool: tuple[str, str] | None,
) -> dict[int, int]:
    """The commands whose rows collide (self/tool/shape), each with the
    first colliding row of its block.

    Runs in the dry-run subprocess against its own checker, replaying BOTH
    boundary streams the dry run recorded — ``select_tool`` and ``set_shapes``
    — so each command is checked with the tool attached and the world active
    at its point in the program (the dry run itself only validates IK).

    The checker's tool and program world are restored on exit: a caller that
    runs the simulation in-process rather than through the pool shares the
    live checker.
    """
    hits: dict[int, int] = {}
    if not robot.has_collision_checking:
        return hits
    from waldoctl import shape_from_wire

    # Unconditional — including the EMPTY set: a reused pool worker keeps its
    # process-global checker between runs, so a cleared world must clear it.
    submit_world = [shape_from_wire(*t) for t in shapes_wire or []]
    tool_key, variant = initial_tool or ("NONE", "")
    try:
        robot.apply_shapes([])
        robot.set_active_tool(tool_key, variant_key=variant or None)
        robot.apply_shapes(submit_world)
        # A boundary recorded on command i applies to the commands after it.
        # Recorded order IS chronological (indexes are non-decreasing) — a sort
        # would reorder same-index back-to-back entries and replay the wrong
        # state.
        tool_bounds = [
            (ts.command, ts.tool_key, ts.variant_key) for ts in tool_selections
        ]
        shape_bounds = [(sc.command, sc.shapes) for sc in shape_changes]
        ti = si = 0
        for block in record.blocks:
            while ti < len(tool_bounds) and tool_bounds[ti][0] < block.command:
                _, b_tool, b_variant = tool_bounds[ti]
                robot.set_active_tool(b_tool, variant_key=b_variant or None)
                ti += 1
            while si < len(shape_bounds) and shape_bounds[si][0] < block.command:
                robot.apply_shapes(list(shape_bounds[si][1]))
                si += 1
            if block.rows == 0:
                continue
            rows = record.joints_rad[block.start_row : block.start_row + block.rows]
            hit = robot.check_trajectory(np.asarray(rows, dtype=np.float64))
            if hit >= 0:
                hits[block.command] = int(hit)
    finally:
        robot.apply_shapes([])
        robot.set_active_tool(tool_key, variant_key=variant or None)
        robot.apply_shapes(submit_world)
    return hits


def _simulation_timeout_s() -> float:
    """Wall-clock budget for one preview run.

    ``WALDO_SIM_TIMEOUT_S`` raises it where a worker starts cold: on spawn
    platforms the first preview a worker runs imports the backend before the
    script does, and that import is not free.
    """
    return float(os.environ.get("WALDO_SIM_TIMEOUT_S", SIMULATION_TIMEOUT_S))


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

    Skipped under pytest: NiceGUI's fixtures reset the pool after every UI
    test, so warming would pay a full import per test instead of once per
    session. A test's first preview pays its own worker's import, which
    ``WALDO_SIM_TIMEOUT_S`` gives it room for.

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
    backend_package: str = "parol6",
    revision: int = 0,
    tool_meta_registry: dict[str, dict] | None = None,
    shapes_wire: list[tuple] | None = None,
    initial_tool: tuple[str, str] | None = None,
    initial_homed: bool = True,
    setup_directory: str | None = None,
    simulate_seconds: float | None = None,
    attachment_epoch: int = 0,
) -> dict[str, Any]:
    """
    Run dry-run simulation in isolated subprocess.

    This function is designed to be called via run.cpu_bound() for process
    isolation. It returns serializable results rather than modifying
    global state.

    The simulation starts with the submitted tool and world snapshot.
    Valid held declarations are bound to the isolated preview's own context.
    Stale declarations require reconciliation before preview.

    Args:
        program_text: The Python program to simulate
        initial_joints_rad: Initial joint angles in radians (robot's current position)
        backend_package: Backend package name for module shimming; its
            ``Robot`` builds the dry-run clients the program runs against
        revision: The program revision this pass answers, handed back so
            the caller can pair it with the plan on screen
        tool_meta_registry: Mapping of tool_key → {motions, variants, activation_type}
        simulate_seconds: When set, the program is also RUN — the backend
            drives the same commands through its control loop against a
            physics plant — and the result carries the predicted record.
            The value bounds SIMULATED time, so a program that never
            terminates still comes back. None plans only.

    Returns:
        Dict with keys:
        - revision: the revision handed in
        - commanded: the commanded record, blocks labelled by line
        - predicted: the predicted record, or None
        - notes: one CommandNote per command
        - targets, tool_actions, tool_selections, shape_changes: what the
          program declared, boundaries indexed by command
        - collisions: {command: first colliding row} from the local checker
        - error: Error message if simulation failed, else None
        - unresolved: whether the program needs an observation fixture
        - physics_error: why the predicted pass failed, if it did
        - final_joints_rad: where the program leaves the arm
    """
    # Collectors local to this subprocess, not shared with the main process.
    local_targets: list[dict] = []
    local_tool_actions: list = []
    local_tool_selections: list = []
    local_shape_changes: list = []
    error_message: str | None = None
    unresolved = False

    import importlib

    from waldo_commander.services.path_preview_client import (
        PathPreviewClient,
        AsyncPathPreviewClient,
    )

    # Lets us read the records after execution.
    created_clients: list[PathPreviewClient] = []
    # (module, attribute, original) for every backend client name swapped for
    # a preview class below, so the thread fallback can put them back.
    swapped_names: list[tuple[Any, str, Any]] = []

    try:
        # Swap RobotClient/AsyncRobotClient for preview clients while the
        # script runs. A pool worker is thrown away afterwards, but this
        # function is also called directly, in-process, so the swap is undone
        # below rather than left to the worker's exit.
        backend = importlib.import_module(backend_package)

        from waldo_commander.profiles import get_robot

        # The backend the program plans against: its own dry-run clients,
        # which carry it, so a skill's requirements are checked for real.
        _preview_robot = get_robot(backend_package)

        def _dr_cls(**kwargs: Any) -> Any:
            return _preview_robot.create_dry_run_client(**kwargs)

        def seed_world(preview: PathPreviewClient) -> None:
            from dataclasses import replace
            from waldoctl import shape_from_wire

            if initial_tool is not None:
                preview.select_tool(initial_tool[0], variant_key=initial_tool[1])
            shapes = [shape_from_wire(*t) for t in shapes_wire or []]
            if not shapes:
                return
            context = preview._client.shapes()
            if context is None:
                raise ValueError("Preview world readback is unavailable")
            bound = []
            for shape in shapes:
                if shape.attachment is not None:
                    if shape.attachment.epoch != attachment_epoch:
                        raise ValueError(
                            "Attachment context is stale; reconcile the scene before preview"
                        )
                    shape = replace(
                        shape,
                        attachment=replace(
                            shape.attachment, epoch=context.attachment_epoch
                        ),
                    )
                bound.append(shape)
            if preview._client.set_shapes(bound) != 1:
                raise ValueError("Preview world application was not confirmed")

        class LocalPathPreviewClient(PathPreviewClient):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(
                    target_collector=local_targets,
                    tool_action_collector=local_tool_actions,
                    tool_selection_collector=local_tool_selections,
                    shape_change_collector=local_shape_changes,
                    initial_joints=initial_joints_rad,
                    initial_homed=initial_homed,
                    dry_run_client_cls=_dr_cls,
                    tool_meta_registry=tool_meta_registry,
                    robot=_preview_robot,
                )
                created_clients.append(self)
                seed_world(self)

        class LocalAsyncPathPreviewClient(AsyncPathPreviewClient):
            def __init__(self, *args: Any, **kwargs: Any):
                self._sync_client = PathPreviewClient(
                    target_collector=local_targets,
                    tool_action_collector=local_tool_actions,
                    tool_selection_collector=local_tool_selections,
                    shape_change_collector=local_shape_changes,
                    initial_joints=initial_joints_rad,
                    initial_homed=initial_homed,
                    dry_run_client_cls=_dr_cls,
                    tool_meta_registry=tool_meta_registry,
                    robot=_preview_robot,
                )
                created_clients.append(self._sync_client)
                seed_world(self._sync_client)

        for module in (backend, getattr(backend, "client", None)):
            if module is None:
                continue
            for name, preview_cls in (
                ("RobotClient", LocalPathPreviewClient),
                ("AsyncRobotClient", LocalAsyncPathPreviewClient),
            ):
                swapped_names.append((module, name, getattr(module, name, None)))
                setattr(module, name, preview_cls)

        # Reset this worker's program-layer world to the submit-time truth
        # BEFORE the script runs: a reused pool worker's process-global checker
        # otherwise carries a previous run's shapes into this run's planning
        # guard. Empty included. Installation shapes come from robot config at
        # backend import and are untouched.
        if _preview_robot.has_collision_checking:
            _preview_robot.apply_shapes([])

        # Inserted into sys.modules so `import time` returns this mock. The
        # mock behavior is scoped to the simulating thread: in the thread
        # fallback the app's event loop keeps running concurrently and must
        # keep seeing real clocks (in a pool worker there is only one thread,
        # so the scoping is a no-op).
        sim_thread_id = threading.get_ident()

        class MockTimeModule(ModuleType):
            """The program's clock, running on simulated time.

            A preview covers a minute of robot time in a fraction of a
            second, so the host clock answers a different question than
            the script is asking. Every reader here answers in simulated
            seconds instead: sleeping advances it, and so does each move,
            which is what lets ``while time.monotonic() - t0 < 5:``
            terminate. It used to answer a constant zero, and such a loop
            spun until the pool timeout killed it.

            Scoped to the simulating thread: in the thread fallback the
            app's event loop runs concurrently and must keep seeing a
            real clock (in a pool worker there is one thread, so the
            scoping costs nothing).
            """

            def __init__(self, real_time_module):
                super().__init__("time")
                self.__file__ = "<mock_time>"
                self.__package__ = ""
                self._real_time = real_time_module

            def __getattr__(self, name):
                return getattr(self._real_time, name)

            def _elapsed(self) -> float:
                return max((c.sim_time_s for c in created_clients), default=0.0)

            def sleep(self, seconds):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.sleep(seconds)
                for client in created_clients:
                    client.record_sleep(seconds)

            def time(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.time()
                return self._elapsed()

            def monotonic(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.monotonic()
                return self._elapsed()

            def perf_counter(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.perf_counter()
                return self._elapsed()

            def perf_counter_ns(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.perf_counter_ns()
                return int(self._elapsed() * 1e9)

            def time_ns(self):
                if threading.get_ident() != sim_thread_id:
                    return self._real_time.time_ns()
                return int(self._elapsed() * 1e9)

        original_time_module = sys.modules.get("time")
        mock_time = MockTimeModule(original_time_module)
        sys.modules["time"] = mock_time

        sim_globals = {
            "__name__": "__main__",
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

            from waldo_commander.setup import using_setup_directory

            with using_setup_directory(setup_directory):
                exec(code, sim_globals)

        except UnresolvedPreview as e:
            unresolved = True
            error_message = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        except Exception as e:
            error_message = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

        finally:
            if original_time_module is not None:
                sys.modules["time"] = original_time_module
            elif "time" in sys.modules and sys.modules["time"] is mock_time:
                del sys.modules["time"]

    except Exception as e:
        error_message = f"Simulation setup failed: {type(e).__name__}: {e}"

    finally:
        # A pool worker is discarded with these swaps in place, but a direct
        # in-process call shares the app's interpreter, where a client built
        # from the backend's name after this point has to be the real one
        # again. In a ``finally`` because a script ending in ``sys.exit()``
        # raises SystemExit, which passes both excepts and would otherwise
        # leave the preview class installed for the rest of the app's life.
        for module, name, original in reversed(swapped_names):
            if original is None:
                delattr(module, name)
            else:
                setattr(module, name, original)

    # Close blend holds and note the last commands, covering scripts without
    # context managers.
    for c in created_clients:
        c.close()

    for c in created_clients:
        if c.accumulated_errors:
            errors_text = "\n".join(c.accumulated_errors)
            if error_message:
                error_message += "\n" + errors_text
            else:
                error_message = errors_text

    # The program's records come off the last client the script built:
    # what it commanded, and — on the predicted pass — what the arm would
    # do, from the same client, so nothing is re-executed and no script
    # runs twice. A failure of the predicted pass costs the physics, not
    # the plan.
    commanded: TickIndex | None = None
    predicted: TickIndex | None = None
    notes: list[CommandNote] = []
    physics_error: str | None = None
    final_joints_rad: list[float] | None = None
    collisions: dict[int, int] = {}
    if created_clients:
        client = created_clients[-1]
        notes = list(client.notes)
        try:
            commanded = _portable(client.plan())
        except Exception as e:
            logger.warning("Reading the commanded record failed: %s", e)
            error_message = (error_message + "\n" if error_message else "") + (
                f"{type(e).__name__}: {e}"
            )
        if commanded is not None:
            if commanded.rows:
                final_joints_rad = commanded.joints_rad[-1].astype(float).tolist()
            for block in commanded.blocks:
                if block.error is not None and block.rows == 0:
                    line = (
                        notes[block.command].line_number
                        if block.command < len(notes)
                        else 0
                    )
                    text = f"Line {line}: {block.error}"
                    if not error_message or text not in error_message:
                        error_message = (
                            error_message + "\n" if error_message else ""
                        ) + text
            # Collision marking runs here (normally a subprocess) so 1000s
            # of C++ checks never block the UI event loop and mid-script tool
            # selections are honored. A marking failure must not discard an
            # otherwise-good dry run.
            try:
                from waldo_commander.profiles import get_robot

                collisions = _mark_colliding_commands(
                    get_robot(backend_package),
                    commanded,
                    local_tool_selections,
                    local_shape_changes,
                    shapes_wire,
                    initial_tool,
                )
            except Exception as e:
                logger.warning("Preview collision marking failed: %s", e)
        if simulate_seconds is not None and commanded is not None:
            try:
                predicted = _portable(client.simulate(simulate_seconds))
            except Exception as e:
                physics_error = f"{type(e).__name__}: {e}"
                logger.warning("Physics simulation failed: %s", e)

    return {
        "revision": revision,
        "commanded": commanded,
        "predicted": predicted,
        "notes": notes,
        "targets": local_targets,
        "tool_actions": local_tool_actions,
        "tool_selections": local_tool_selections,
        "shape_changes": local_shape_changes,
        "collisions": collisions,
        "error": error_message,
        "unresolved": unresolved,
        "physics_error": physics_error,
        "final_joints_rad": final_joints_rad,
    }


def _portable(record: TickIndex) -> TickIndex:
    """The record as it crosses the process boundary: a block's refusal is
    a backend error object that may not pickle, and then its text is what
    the host gets."""
    blocks = []
    for block in record.blocks:
        error = block.error
        if error is not None:
            try:
                pickle.loads(pickle.dumps(error))
            except Exception:
                error = str(error)
        blocks.append(replace(block, error=error))
    record.blocks = tuple(blocks)
    return record


def _run_simulation_packed(args: tuple) -> dict[str, Any]:
    """Single-argument adapter for run.cpu_bound, whose ParamSpec cannot type
    a heterogeneous *args unpack; packing also lets the pool call and the
    in-process fallback share one argument list."""
    return _run_simulation_isolated(*args)


class _PhysicsPool:
    """One worker, ours to kill.

    The physics pass is seconds of solid CPU, and a superseding edit has
    to be able to abandon it immediately. NiceGUI's shared pool can be
    killed but only wholesale — every worker and every other in-flight
    job with it — so this owns a pool of one instead. Cancelling means
    killing the process and letting the next submission spawn a fresh
    one, which costs a backend import and disturbs nothing else.
    """

    def __init__(self) -> None:
        self._pool: ProcessPoolExecutor | None = None
        self._current: asyncio.Future | None = None

    def _ensure(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn"),
                max_tasks_per_child=1,
            )
        return self._pool

    async def run(self, fn: Callable, args: tuple) -> Any:
        """Run *fn*, abandoning whatever was running before it."""
        self.cancel()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._ensure(), fn, args)
        self._current = future
        try:
            return await future
        except asyncio.CancelledError:
            # wait_for() cancels the caller before its timeout handler can
            # run; the worker must be terminated while we still own it.
            if self._current is future:
                self.cancel()
            raise
        finally:
            if self._current is future:
                self._current = None

    def cancel(self) -> None:
        """Abandon the run in flight, if any.

        A future already executing does not stop when cancelled — the
        worker keeps burning CPU until it finishes — so the process goes
        with it.
        """
        current, self._current = self._current, None
        if current is None:
            return
        current.cancel()
        pool, self._pool = self._pool, None
        if pool is not None:
            for p in getattr(pool, "_processes", {}).values():
                p.kill()
            pool.shutdown(wait=False)

    def shutdown(self) -> None:
        self.cancel()
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)


class PathVisualizer:
    """Visualizes robot path from program simulation."""

    def __init__(self):
        self._simulation_lock = asyncio.Lock()
        self._simulation_count = 0
        self._physics = _PhysicsPool()
        # The exact arguments each tab was last PLANNED with, and the
        # revision they answered. The predicted pass runs seconds later and
        # must answer that plan, not whatever the world happens to look
        # like by the time it starts.
        self._planned_args: dict[str, tuple[tuple, int]] = {}
        # Which tabs have a predicted pass in flight.
        self._physics_tabs: set[str] = set()
        # Whether a backend's predicted record has ever differed from its
        # commanded one, per backend package, for this session. A planner
        # with no plant hands back the same record twice; after the first
        # time it does, its predicted pass is not run again. Observed, not
        # asked: nothing on the wire says what a pass will carry.
        self._predicted_diverges: dict[str, bool] = {}

    def reset_for_test(self) -> None:
        """Rebuild loop-bound state so the next test's event loop starts clean.

        ``asyncio.Lock`` binds to a loop the first time it is acquired while
        already held, and stays bound. A simulation still in flight when its
        test ends leaves this lock acquired against a loop that is about to
        close, so the next test's simulation takes the contended path and
        raises ``bound to a different event loop``.
        """
        self._physics.shutdown()
        type(self).__init__(self)

    def physics_in_flight(self, tab_id: str | None) -> bool:
        """Whether a predicted pass is running for *tab_id*."""
        return tab_id is not None and tab_id in self._physics_tabs

    def _simulation_args(
        self,
        program_text: str,
        robot: Any,
        revision: int = 0,
        simulate_seconds: float | None = None,
    ) -> tuple | None:
        """Everything a preview worker needs, or None when this backend
        cannot preview at all.

        Both passes run the same program against the same world, tool and
        starting pose — the only difference is whether the worker also
        simulates — so they are built here once rather than kept in step
        by hand.
        """
        # Current robot joint angles seed the simulation's initial position.
        initial_joints_rad: np.ndarray | None = None
        if len(waldoctl.commander.status.joints.angles) >= robot.joints.count:
            initial_joints_rad = waldoctl.commander.status.joints.angles.rad.copy()
            logger.debug(
                "Using current robot joints as initial: %s deg",
                waldoctl.commander.status.joints.angles.deg,
            )

        backend_pkg = robot.backend_package
        if robot.create_dry_run_client() is None:
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
                base_motions = _serialize_motions(spec.motions) if spec.motions else []
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

        from waldo_commander.setup import SetupStore

        return (
            program_text,
            initial_joints_rad,
            backend_pkg,
            revision,
            tool_meta_registry or None,
            shapes_wire,
            initial_tool,
            initial_homed,
            str(SetupStore().directory),
            simulate_seconds,
            scene_handle.attachment_epoch if scene_handle is not None else 0,
        )

    async def update_path_visualization(
        self, program_text: str, tab_id: str | None = None, revision: int = 0
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
            revision: The program revision this plan answers; the predicted
                pass that follows carries the same one.

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

            sim_args = self._simulation_args(
                program_text, ui_state.active_robot, revision
            )
            if sim_args is None:
                simulation_state.notify_changed()
                return None
            # The simulated program always runs in a pool worker, which is
            # discarded afterwards. It mutates process globals — the time
            # module, the collision checker's world, the backend's client
            # classes — so running it here would leak every one of them into
            # the app, and a runaway script would be unkillable in a thread.
            # Without a pool there is no preview, which is the honest answer.
            if run.process_pool is None:
                logger.error("No simulation process pool (sim_id=%d)", sim_id)
                return "Preview unavailable: no simulation process pool"
            try:
                result = await asyncio.wait_for(
                    run.cpu_bound(_run_simulation_packed, sim_args),
                    # Over the script's own budget, for process overhead.
                    timeout=_simulation_timeout_s() + 2.0,
                )
            except asyncio.TimeoutError:
                logger.error("Simulation subprocess timed out (sim_id=%d)", sim_id)
                return "Simulation timed out"
            except Exception as e:
                logger.error("Subprocess simulation failed (sim_id=%d): %s", sim_id, e)
                return f"Simulation failed: {e}"

            # A None result can happen during shutdown/test teardown.
            if result is None:
                logger.warning("Simulation returned None result (sim_id=%d)", sim_id)
                return "Simulation returned no result"

            if result.get("error"):
                report = logger.warning if result.get("unresolved") else logger.error
                report("Simulation error (sim_id=%d): %s", sim_id, result["error"])

            commanded: TickIndex | None = result.get("commanded")
            logger.info(
                "Simulation complete (sim_id=%d): %d commands, %d rows.",
                sim_id,
                len(commanded.blocks) if commanded is not None else 0,
                commanded.rows if commanded is not None else 0,
            )

            # Store results in the originating tab, falling back to the active tab.
            target_tab = None
            if tab_id:
                target_tab = waldoctl.commander.programs.get(tab_id)
            if not target_tab:
                target_tab = waldoctl.commander.programs.active

            if target_tab:
                previous = self._planned_args.get(target_tab.id)
                same_inputs = previous is not None and pickle.dumps(
                    previous[0]
                ) == pickle.dumps(sim_args)
                # The predicted pass answers exactly this plan, seconds
                # later; a plan that never produced a record leaves nothing
                # to answer.
                if commanded is None:
                    self._planned_args.pop(target_tab.id, None)
                else:
                    self._planned_args[target_tab.id] = (sim_args, revision)
                dry_run = target_tab.dry_run

                # Always store final_joints_rad (used for position-change
                # detection even when the plan is unchanged).
                dry_run.final_joints_rad = result.get("final_joints_rad")

                # An identical record paints an identical picture: skip the
                # update to avoid a scrub bar rebuild and a visual flash. Not
                # when there's an error — the caller needs the error string
                # to apply diagnostics even if the record is the same.
                if (
                    commanded is not None
                    and dry_run.commanded is not None
                    and commanded.digest
                    and dry_run.commanded.digest == commanded.digest
                    and not result.get("error")
                    and same_inputs
                ):
                    # The plan on screen answers this revision too, and so
                    # does the predicted record that answered it.
                    if dry_run.predicted_revision == dry_run.commanded_revision:
                        dry_run.predicted_revision = revision
                    dry_run.commanded_revision = revision
                    logger.info(
                        "Simulation results unchanged (sim_id=%d), skipping update",
                        sim_id,
                    )
                    return UNCHANGED

                self._apply_plan(dry_run, result, revision)

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

    @staticmethod
    def _apply_plan(dry_run: Any, result: dict[str, Any], revision: int) -> None:
        """Put a commanded record and what the scene draws from it on the
        program. A new plan retires the predicted record that answered the
        old one: until the next pass lands, predicted is commanded."""
        commanded: TickIndex | None = result.get("commanded")
        notes = list(result.get("notes") or [])
        tool_actions = list(result.get("tool_actions") or [])
        tool_selections = list(result.get("tool_selections") or [])
        shape_changes = list(result.get("shape_changes") or [])
        targets = [ProgramTarget.from_dict(d) for d in result.get("targets") or []]
        if commanded is None:
            segments = []
        else:
            segments = segments_from_record(
                commanded, notes, result.get("collisions") or {}
            )
            index_boundaries(segments, tool_selections)
            index_boundaries(segments, shape_changes)
            tool_actions = tool_actions_from_record(tool_actions, commanded, segments)
        dry_run.commanded = commanded
        dry_run.commanded_revision = revision
        dry_run.predicted = None
        dry_run.predicted_revision = -1
        dry_run.commands = notes
        dry_run.path_segments = segments
        dry_run.targets = targets
        dry_run.tool_actions = tool_actions
        dry_run.tool_selections = tool_selections
        dry_run.total_steps = len(segments)

    async def update_physics_simulation(self, tab_id: str | None = None) -> str | None:
        """Run the planned program through the backend's simulation for the
        predicted record.

        The planning pass has already returned by the time this starts,
        so the user is looking at the commanded path while this fills in
        what the arm would do. Nothing waits on it: playback scrubs the
        commanded record meanwhile, and the predicted one takes over when
        it lands.

        It replays the plan's OWN arguments — same program text, same
        world, same tool, same starting pose — rather than rebuilding
        them from live state seconds later. A keep-out added during the
        wait would otherwise make the record describe a different world
        than the plan on screen, with nothing to notice it. The record
        lands only if the plan it answers is still the one on screen.

        A backend whose prediction has been seen to be its plan — the same
        record twice, with nothing a plant would add — is not asked again
        this session.
        """
        tab = (
            waldoctl.commander.programs.get(tab_id)
            if tab_id
            else waldoctl.commander.programs.active
        )
        if tab is None:
            return None
        planned = self._planned_args.get(tab.id)
        if planned is None:
            return None  # nothing planned to answer
        args, revision = planned
        backend = ui_state.active_robot.backend_package
        if self._predicted_diverges.get(backend) is False:
            return None
        bound = inspect.signature(_run_simulation_isolated).bind(*args)
        bound.arguments["simulate_seconds"] = MAX_SIMULATED_SECONDS
        args = bound.args

        self._physics_tabs.add(tab.id)
        try:
            result = await asyncio.wait_for(
                self._physics.run(_run_simulation_packed, args),
                timeout=PHYSICS_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            # Not an outcome. Superseded work must unwind, not return and
            # let the caller carry on mutating state that has moved on.
            raise
        except asyncio.TimeoutError:
            self._physics.cancel()
            logger.warning("Physics simulation timed out; the worker was killed")
            return "Physics simulation timed out"
        except Exception as e:
            logger.warning("Physics simulation failed: %s", e)
            return str(e)
        finally:
            self._physics_tabs.discard(tab.id)

        predicted: TickIndex | None = (result or {}).get("predicted")
        if predicted is None:
            return (result or {}).get("error") or (result or {}).get("physics_error")
        dry_run = tab.dry_run
        if dry_run.commanded_revision != (result or {}).get("revision"):
            logger.debug("Predicted record answers a superseded plan; dropped")
            return None
        commanded = dry_run.commanded
        diverges = commanded is None or (
            predicted.digest != commanded.digest or bool(predicted.channels)
        )
        self._predicted_diverges[backend] = diverges
        # The backend guarantees the same program gives a bit-identical
        # record, so an equal digest means an identical picture and the
        # scene keeps what it has. This is the flash guard.
        previous = dry_run.predicted
        if (
            previous is not None
            and predicted.digest
            and previous.digest == predicted.digest
            and dry_run.predicted_revision == dry_run.commanded_revision
        ):
            return None
        dry_run.predicted = predicted
        dry_run.predicted_revision = dry_run.commanded_revision
        logger.info(
            "Physics simulation complete: %d rows over %.2f s (%s)",
            predicted.rows,
            predicted.duration_s,
            predicted.stop,
        )
        simulation_state.notify_changed()
        return None

    def cancel_physics(self) -> None:
        """Abandon a physics pass in flight — a superseding edit landed.

        Kills the worker and nothing else. This also runs at page
        teardown, after the host has torn the commander down, so it must
        not reach for any application state; the pending flag is cleared
        by whoever set it.
        """
        self._physics.cancel()

    def forget_plan(self, tab_id: str) -> None:
        """Drop a tab's stored plan arguments — there is no plan to answer."""
        self._planned_args.pop(tab_id, None)


path_visualizer = PathVisualizer()
