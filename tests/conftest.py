"""Pytest configuration and shared fixtures for Waldo Commander tests."""

import logging
import os
import random
import subprocess
import sys
import asyncio
from collections.abc import Generator
from typing import TYPE_CHECKING

import numpy as np
import pytest
from nicegui import run as nicegui_run
from nicegui.testing import general as nicegui_testing_general
from nicegui.testing.general_fixtures import (
    nicegui_reset_globals,  # noqa: F401 - required by screen fixture
)
from nicegui.testing import screen_plugin as nicegui_screen_plugin
from nicegui.testing.screen import Screen
from nicegui.testing.screen_plugin import (
    nicegui_driver,  # noqa: F401 - default driver (per-test browser)
    nicegui_remove_all_screenshots,  # noqa: F401 - clears screenshots before session
    pytest_runtest_makereport,  # noqa: F401
    screen,  # noqa: F401 - default screen fixture (creates browser per test)
)
import waldoctl
from parol6 import Robot
from parol6.config import HOME_ANGLES_DEG
from selenium import webdriver as _webdriver

# SSH X11 forwarding gotcha: when pytest is run over SSH with X forwarding
# enabled, DISPLAY is set to "localhost:N.0" pointing at the forwarded
# tunnel. Headless Chromium honors DISPLAY even with --headless=new and
# tries to use that "X server" for GL context creation, but the SSH tunnel
# has no hardware GL extensions — every WebGL surface ends up with no
# context at all, so three.js scenes never initialize and every WebGL
# screen test times out at the data-initializing wait. Headless mode uses
# /dev/dri directly via Mesa and does not need any X server, so unset
# DISPLAY at import time. We narrow the check to localhost: prefix so a
# real local X server (DISPLAY=":0", Unix socket, or path-based) is left
# untouched, and HEADED=1 mode is preserved entirely because the developer
# explicitly wants a real X window.
if not os.environ.get("HEADED") and os.environ.get("DISPLAY", "").startswith(
    "localhost:"
):
    os.environ.pop("DISPLAY", None)

if TYPE_CHECKING:
    from parol6 import AsyncRobotClient

# ============================================================================
# Skip marker for WebGL-dependent tests on macOS CI
# ============================================================================
# SwiftShader WebGL fails with "BindToCurrentSequence failed" on macOS runners
skip_webgl_macos_ci = pytest.mark.skipif(
    sys.platform == "darwin" and "GITHUB_ACTIONS" in os.environ,
    reason="WebGL context creation fails on macOS CI with SwiftShader",
)

# ============================================================================
# Port Configuration (session-randomized to avoid conflicts)
# ============================================================================
# Generate unique ports per test session to avoid conflicts between test runs
_SESSION_PORT_BASE = random.randint(10000, 50000)
CONTROLLER_PORT = _SESSION_PORT_BASE
MULTICAST_PORT = _SESSION_PORT_BASE + 1


def _get_test_ports() -> tuple[int, int]:
    """Get the session-unique ports for controller and multicast."""
    return CONTROLLER_PORT, MULTICAST_PORT


@pytest.fixture(scope="session")
def nicegui_chrome_options():
    """Chrome options for screen tests (overrides NiceGUI default).

    Differs from NiceGUI's built-in:
    - HEADED=1 env var skips headless (NiceGUI always adds it)
    - GL via ANGLE for WebGL/three.js tests (NiceGUI disables GPU in CI)
    """
    options = _webdriver.ChromeOptions()
    options.add_argument("disable-dev-shm-usage")
    options.add_argument("disable-search-engine-choice-screen")
    options.add_argument("no-sandbox")
    if not os.environ.get("HEADED"):
        options.add_argument("headless=new")
    options.add_argument("--use-gl=angle")
    if "GITHUB_ACTIONS" in os.environ:
        # GitHub runners have no GPU — force SwiftShader CPU rasterizer.
        options.add_argument("--use-angle=swiftshader-webgl")
    elif sys.platform == "linux" and os.access(
        "/dev/dri/renderD128", os.R_OK | os.W_OK
    ):
        # Linux with an accessible DRM render node — force ANGLE's GL/EGL
        # backend so it picks up Mesa (or NVIDIA EGL) via /dev/dri instead
        # of falling back to SwiftShader. Linux's default ANGLE backend is
        # Vulkan, and the only Vulkan ICD bundled with Chromium is
        # SwiftShader — that's how the silent software fallback happens.
        # Headless mode does NOT need a DISPLAY for this; the render group
        # on the user is enough. macOS and Windows already use Metal/D3D11
        # by default and don't need a backend hint.
        options.add_argument("--use-angle=gl-egl")
    options.add_argument(f"window-size={TEST_WINDOW_WIDTH},{TEST_WINDOW_HEIGHT}")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    return options


# Window size for screen tests - full HD for proper layout
TEST_WINDOW_WIDTH = 1920
TEST_WINDOW_HEIGHT = 1080


@pytest.fixture(autouse=True)
def set_screen_window_size(
    request: pytest.FixtureRequest,
) -> None:
    """Set browser window size and page load timeout for screen tests.

    This ensures consistent layout across all browser tests.
    Only runs when a test actually uses the screen fixture.
    """
    if "screen" not in request.fixturenames:
        return
    screen_fixture: Screen = request.getfixturevalue("screen")
    screen_fixture.selenium.set_window_size(TEST_WINDOW_WIDTH, TEST_WINDOW_HEIGHT)
    screen_fixture.selenium.set_page_load_timeout(
        8
    )  # WebGL needs more time on slow runners


@pytest.fixture(scope="session", autouse=True)
def silence_noisy_logging():
    """Reduce verbosity of noisy third-party loggers.

    Selenium debug output includes base64-encoded screenshots.
    Numba debug output floods with SSA/IR details during JIT compilation.
    """
    logging.getLogger("selenium").setLevel(logging.INFO)
    logging.getLogger("selenium.webdriver").setLevel(logging.INFO)
    logging.getLogger("selenium.webdriver.remote").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
    # Silence numba JIT compilation debug spam
    logging.getLogger("numba").setLevel(logging.WARNING)
    logging.getLogger("numba.core").setLevel(logging.WARNING)
    # Silence toppra debug output
    logging.getLogger("toppra").setLevel(logging.WARNING)
    yield


class _ProactorWriteErrorFilter(logging.Filter):
    """Suppress Windows ProactorEventLoop 'Fatal write error' on datagram transport.

    On Windows, Python's ProactorEventLoop can emit a spurious ERROR when a
    UDP datagram transport has a pending overlapped write that fires after
    the proactor is torn down between tests.  This is a known CPython
    limitation and is harmless -- the transport is already being closed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "Fatal write error on datagram transport" not in record.getMessage()


@pytest.fixture(scope="session", autouse=True)
def suppress_proactor_write_error(silence_noisy_logging):
    """Filter the Windows-specific ProactorEventLoop datagram write error.

    Prevents the harmless asyncio ERROR from triggering NiceGUI's
    'unexpected ERROR logs' test failure detection.
    """
    if sys.platform != "win32":
        yield
        return

    asyncio_logger = logging.getLogger("asyncio")
    filt = _ProactorWriteErrorFilter()
    asyncio_logger.addFilter(filt)
    yield
    asyncio_logger.removeFilter(filt)


# ============================================================================
# Class-scoped Browser Fixture for Expensive Browser Tests
# ============================================================================


@pytest.fixture(scope="class")
def class_driver(
    request: pytest.FixtureRequest,
) -> Generator[_webdriver.Chrome, None, None]:
    """Class-scoped Chrome webdriver for shared browser tests.

    Creates a single browser instance that persists across all tests in a class.
    CSS animations are disabled for deterministic testing.
    """
    from selenium.webdriver.chrome.service import Service
    import shutil

    options = _webdriver.ChromeOptions()
    if not os.environ.get("HEADED"):
        options.add_argument("headless=new")
    options.add_argument("disable-search-engine-choice-screen")
    options.add_argument("--use-gl=angle")
    # ANGLE backend selection — see nicegui_chrome_options for the rationale.
    if "GITHUB_ACTIONS" in os.environ:
        options.add_argument("--use-angle=swiftshader-webgl")
    elif sys.platform == "linux" and os.access(
        "/dev/dri/renderD128", os.R_OK | os.W_OK
    ):
        options.add_argument("--use-angle=gl-egl")
    options.add_argument("no-sandbox")
    options.add_argument("disable-dev-shm-usage")
    # Disable CSS animations for deterministic testing
    options.add_argument("--disable-animations")

    # Find system chromedriver (same as NiceGUI's approach)
    chromedriver_path = shutil.which("chromedriver")
    if chromedriver_path:
        service = Service(executable_path=chromedriver_path)
        driver = _webdriver.Chrome(service=service, options=options)
    else:
        driver = _webdriver.Chrome(options=options)

    driver.set_window_size(TEST_WINDOW_WIDTH, TEST_WINDOW_HEIGHT)
    driver.implicitly_wait(0)

    yield driver

    driver.quit()


class _StubCaplog:
    """Minimal caplog stub for class-scoped screen fixture."""

    def __init__(self):
        self.records = []

    def clear(self):
        self.records = []


@pytest.fixture(scope="class")
def class_screen(
    request: pytest.FixtureRequest,
    class_driver: _webdriver.Chrome,
) -> Generator["Screen", None, None]:
    """Browser session shared across all tests in a class.

    Use for expensive browser tests that don't need isolation between tests.
    The browser navigates to the app once at class setup and stays open.

    Usage:
        @pytest.mark.browser
        class TestPanelOperations:
            def test_first(self, class_screen):
                # Uses shared browser session
                ...

            def test_second(self, class_screen):
                # Same browser session, state persists from test_first
                ...
    """
    # Set the port env var that NiceGUI's ui.run() expects for screen tests
    os.environ["NICEGUI_SCREEN_TEST_PORT"] = str(Screen.PORT)

    try:
        # Reset NiceGUI globals at class setup (isolation between classes)
        with nicegui_testing_general.nicegui_reset_globals():
            # Create Screen wrapper with class-scoped driver (stub caplog since we share session)
            screen_instance = Screen(class_driver, _StubCaplog(), request)  # type: ignore[arg-type]

            # Set storage keys to bypass first-time dialogs before opening app
            from nicegui import app as ng_app

            from waldo_commander.components.help_menu import HelpMenu

            ng_app.storage.general[HelpMenu.FIRST_VISIT_KEY] = True
            ng_app.storage.general[HelpMenu.SAFETY_ACKNOWLEDGED_KEY] = True

            # Navigate to app once for all tests in class
            # CI with SwiftShader needs more time for WebGL initialization
            screen_instance.open("/", timeout=30.0)

            yield screen_instance

            # Stop server before exiting context
            screen_instance.stop_server()
        # NiceGUI globals reset on context exit (class teardown)
        # Re-setup process pool since nicegui_reset_globals calls run.reset()
        nicegui_run.setup()
    finally:
        os.environ.pop("NICEGUI_SCREEN_TEST_PORT", None)


@pytest.fixture(autouse=True)
def reset_editor_singletons(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Reset module-level editor singletons between tests.

    EditorDecorations, LogPanelController, PlaybackController, SimulationEngine,
    and ScriptExecutionController are constructed once at import time and
    survive across tests. Clear their transient state so each test starts
    from a clean baseline (matches the post-page-load state).

    Skips reset for class_screen tests — those keep the same page alive
    across tests in the class, so wiping the singletons' widget refs would
    desynchronize Python from the still-mounted DOM (button.props() calls
    silently no-op because the field is None).
    """
    if "class_screen" in request.fixturenames:
        yield
        return
    yield
    from waldo_commander.components.editor_decorations import decorations
    from waldo_commander.components.log_panel import log_panel
    from waldo_commander.components.playback import playback
    from waldo_commander.components.simulation_engine import simulation
    from waldo_commander.components.script_execution import script_exec

    # Only playback owns a per-page simulation_state listener; reset it first
    # so its cleanup() removes that listener before the other resets run.
    # The other singletons' resets only re-init their own state.
    playback.reset_for_test()
    decorations.reset_for_test()
    log_panel.reset_for_test()
    simulation.reset_for_test()
    script_exec.reset_for_test()

    # Script/sim flags live on per-program execution / playback state. Without
    # resetting, a test that leaves a program's execution marked running (e.g.
    # crash mid-start) poisons gating checks in the next test.
    try:
        for p in waldoctl.commander.programs.items:
            p.execution.is_running = False
            p.dry_run.playback.executing_step_index = -1
            p.dry_run.playback.executing_step_at_end = False
    except RuntimeError:
        pass


@pytest.fixture(autouse=True)
def restore_process_pool_after_nicegui_fixtures(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Re-setup process pool after tests using NiceGUI's user or screen fixtures.

    These fixtures use nicegui_reset_globals which calls run.reset(),
    clearing the process pool. This fixture re-sets it up after such tests.
    """
    yield
    # Re-setup if this test used user or screen fixture (not class_screen, which handles it)
    uses_nicegui_fixture = "user" in request.fixturenames or (
        "screen" in request.fixturenames and "class_screen" not in request.fixturenames
    )
    if uses_nicegui_fixture:
        nicegui_run.setup()


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers and run the screen plugin's configure hook
    (the screen fixtures are imported, not plugin-registered, so Screen.PORT /
    SCREENSHOT_DIR / DOWNLOAD_DIR must be set up here)."""
    nicegui_screen_plugin.pytest_configure(config)
    config.addinivalue_line(
        "markers", "browser: marks tests that require a real browser (via Selenium)"
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip browser (screen) tests on Windows/macOS CI runners.

    We rely on NiceGUI/Quasar/Vue to provide a consistent cross-platform
    experience, so browser tests only need to run on Linux.
    """
    if sys.platform == "linux" or "GITHUB_ACTIONS" not in os.environ:
        return

    skip = pytest.mark.skip(reason="Browser tests disabled on non-Linux CI runners")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def setup_nicegui_process_pool() -> Generator[None, None, None]:
    """Enable NiceGUI's process pool for cpu_bound() calls in tests.

    This allows tests to use `run.cpu_bound()` for subprocess isolation,
    matching production behavior for path visualization simulations.
    """
    nicegui_run.setup()
    yield
    nicegui_run.reset()


@pytest.fixture(scope="session", autouse=True)
def test_env_config() -> Generator[None, None, None]:
    """Configure environment variables for deterministic test behavior.

    Sets up fake serial and simulator modes so tests can run without hardware.
    These are only set if not already present in the environment.
    """
    controller_port, multicast_port = _get_test_ports()
    env_defaults: dict[str, str] = {
        "PAROL6_FAKE_SERIAL": "1",  # Use fake serial for controller
        "WALDO_WEBAPP_REQUIRE_READY": "1",
        "WALDO_EXCLUSIVE_START": "0",  # Allow reusing session-scoped controller
        "WALDO_LOG_LEVEL": "DEBUG",
        # Connect webapp to the session-randomized controller port
        "WALDO_CONTROLLER_PORT": str(controller_port),
        "PAROL6_CONTROLLER_PORT": str(controller_port),
        "PAROL6_STATUS_MULTICAST_PORT": str(multicast_port),
        # Skip slow envelope generation by default (tests that need it enable explicitly)
        "WALDO_SKIP_ENVELOPE": "1",
        # Reduce status broadcast rate for tests (50Hz is for human-perceived real-time,
        # 20Hz is sufficient for automated tests and reduces CI load)
        "PAROL6_STATUS_RATE_HZ": "20",
    }

    originals: dict[str, str | None] = {}
    for key, default_val in env_defaults.items():
        originals[key] = os.environ.get(key)
        if originals[key] is None:
            os.environ[key] = default_val

    try:
        yield
    finally:
        for key, original_val in originals.items():
            if original_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original_val


@pytest.fixture
def robot_state():
    """Provide access to the shared RobotState instance.

    This exposes `waldo_commander.state.robot_state` so tests can
    prime or inspect global robot state without importing main.py
    and triggering NiceGUI startup handlers a second time.
    """
    from waldo_commander import state as state_module

    return state_module.robot_state


class _StubClient:
    """Raises if a test accidentally reaches for ``commander.client``."""

    def __getattr__(self, name: str) -> object:
        raise NotImplementedError(
            f"_StubClient.{name} — unit tests should not use commander.client"
        )


def _install_test_commander() -> None:
    """Register a minimal ``Commander`` for tests that exercise WC code
    without starting the full NiceGUI app. Idempotent; re-registers a fresh
    Commander each call so per-test isolation matches state resets.
    """
    import waldoctl
    from waldoctl import Commander, RobotStatus, Settings

    from waldo_commander.profiles import get_robot
    from waldo_commander.services.programs import EditorPrograms

    waldoctl._set_commander(
        Commander(
            robot=get_robot(),
            client=_StubClient(),  # type: ignore[arg-type]
            status=RobotStatus(),
            programs=EditorPrograms(),
            settings=Settings(),
        )
    )


@pytest.fixture(autouse=True)
def reset_state(request: pytest.FixtureRequest):
    """Reset all shared state between tests for isolation.

    Skips reset for class_screen tests since the app persists across tests.
    This unified fixture replaces individual reset_* fixtures in test classes.
    """
    # Don't reset state for class_screen tests - app persists across tests
    if "class_screen" in request.fixturenames:
        yield
        return

    from nicegui import app as ng_app

    from waldo_commander import state as state_module
    from waldo_commander.components.help_menu import HelpMenu
    from waldo_commander.state import reset_all_state

    # Mark first visit and safety as acknowledged so dialogs don't appear
    ng_app.storage.general[HelpMenu.FIRST_VISIT_KEY] = True
    ng_app.storage.general[HelpMenu.SAFETY_ACKNOWLEDGED_KEY] = True

    # Reinstall a fresh Commander before reset_all_state() runs — live-app
    # tests' shutdown hook clears the locator, so unit tests that follow
    # would otherwise hit the "not initialised" RuntimeError.
    _install_test_commander()

    reset_all_state()

    # Test-specific overrides (differ from zero defaults). _install_test_commander()
    # above guarantees the locator is registered, so these need no guards.
    from waldoctl import FrameJogAvailability

    waldoctl.commander.status.joints.angles.set_deg(
        np.array(HOME_ANGLES_DEG, dtype=np.float64)
    )
    state_module.robot_state.io = np.array([0, 0, 0, 0, 1], dtype=np.int32)  # ESTOP OK
    io = waldoctl.commander.status.io
    io.inputs = [0, 0]
    io.outputs = [0, 0]
    io.estop = 1
    joints = waldoctl.commander.status.joints
    joints.can_jog_pos = [True] * 6
    joints.can_jog_neg = [True] * 6
    cart_jog = waldoctl.commander.status.pose.cart_jog
    for frame in ("WRF", "TRF"):
        av = cart_jog.by_frame.get(frame)
        if av is None:
            av = FrameJogAvailability()
            cart_jog.by_frame[frame] = av
        av.can_jog_pos = [True] * 6
        av.can_jog_neg = [True] * 6

    # Clear NiceGUI log handler targets to prevent deadlocks when logging
    # triggers widget.push() on stale widgets outside a NiceGUI context
    from waldo_commander.common.logging_config import _ui_log_targets, _ui_lock

    with _ui_lock:
        _ui_log_targets.clear()

    yield


@pytest.fixture(scope="session", autouse=True)
def kill_stale_controllers() -> Generator[None, None, None]:
    """Kill any existing controller processes before and after test session.

    Ensures no stale controllers from previous runs interfere with tests.
    """
    controller_port, _ = _get_test_ports()

    def _kill() -> None:
        try:
            if sys.platform.startswith("linux") or sys.platform == "darwin":
                # Kill all controller processes
                subprocess.run(
                    ["pkill", "-f", "parol6.server.controller"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError):
            pass

    # Pre-session cleanup
    _kill()
    try:
        yield
    finally:
        # Post-session cleanup
        _kill()
        # Best-effort verification (non-fatal)
        try:
            from waldo_commander.constants import config

            probe = Robot(host=config.controller_host, port=controller_port)
            if probe.is_available():
                _kill()
        except (OSError, subprocess.SubprocessError):
            pass


@pytest.fixture(scope="session", autouse=True)
def session_controller(
    test_env_config: None,
    kill_stale_controllers: None,
    warmup_jit_cache: None,
) -> Generator["Robot", None, None]:
    """Session-scoped controller shared by all tests.

    Starts the controller once per test session and keeps it running.
    The app's start_controller() will detect it and reuse it (via WALDO_EXCLUSIVE_START=0).
    This saves ~4 seconds per test (2s start + 2s stop).
    """
    controller_port, multicast_port = _get_test_ports()

    robot = Robot(
        host="127.0.0.1",
        port=controller_port,
        normalize_logs=True,
    )
    robot.start(
        extra_env={"PAROL6_STATUS_MULTICAST_PORT": str(multicast_port)},
    )

    try:
        yield robot
    finally:
        robot.stop()


@pytest.fixture(scope="session", autouse=True)
def warmup_jit_cache(silence_noisy_logging: None) -> None:
    """Pre-warm numba JIT cache before controller starts.

    Without cache, JIT compilation takes 20+ seconds which exceeds the 10s
    controller startup timeout. By warming up first, we populate the cache
    so the controller's warmup is fast.
    """
    from parol6.utils.warmup import warmup_jit

    warmup_jit()


@pytest.fixture(scope="session", autouse=True)
def session_client(
    session_controller: "Robot",
) -> Generator["AsyncRobotClient", None, None]:
    """Session-scoped async client connected to the session controller.

    Performs initial setup (simulator, enable) once per session.
    The controller_reset fixture can be used for per-test reset if needed.
    """
    from parol6 import AsyncRobotClient

    controller_port, _ = _get_test_ports()
    # Use longer timeout for CI environments where scheduling can cause delays
    client = AsyncRobotClient(host="127.0.0.1", port=controller_port, timeout=5.0)

    # Initial setup - wait for controller and enable simulator
    async def setup():
        await client.wait_ready(timeout=10.0)
        await client.simulator(True)
        await client.resume()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(setup())
    finally:
        loop.close()

    try:
        yield client
    finally:
        # Use asyncio.run() for cleaner event loop handling during teardown
        # This avoids issues with new_event_loop() during interpreter shutdown on Python 3.14
        try:
            asyncio.run(client.close())
        except RuntimeError:
            # Event loop may already be closed or unavailable during shutdown
            pass


@pytest.fixture(autouse=True)
async def controller_reset(
    request: pytest.FixtureRequest,
    session_controller: "Robot",
):
    """Per-test fixture that resets the shared controller state.

    Runs automatically before each test that uses user or screen fixtures.
    Much faster than full controller restart (~0.001s vs ~4s).

    Note: class_screen tests share browser state across all tests in the class,
    so we only reset/home once when the class_screen fixture is set up.
    """
    from parol6 import AsyncRobotClient

    # Skip reset for class_screen tests - they share state across tests in a class
    # The controller is reset once when the class_screen fixture sets up
    if "class_screen" in request.fixturenames:
        yield
        return

    # Only reset for tests that use NiceGUI app (user or screen fixture)
    if "user" in request.fixturenames or "screen" in request.fixturenames:
        controller_port, _ = _get_test_ports()
        # Create a fresh client on this test's event loop
        # Use longer timeout for CI environments where scheduling can cause delays
        async with AsyncRobotClient(
            host="127.0.0.1", port=controller_port, timeout=5.0
        ) as client:
            await client.reset()
            await client.resume()
            # Home the robot to ensure valid joint angles (0.0 is invalid for some joints)
            # Use short timeouts since simulator homing is instant
            await client.home(wait=True, timeout=10.0)

    yield


@pytest.fixture
def enable_envelope() -> Generator[None, None, None]:
    """Enable envelope generation for tests that specifically need it.

    By default, WALDO_SKIP_ENVELOPE=1 is set to speed up tests.
    Use this fixture for tests that verify envelope functionality.
    """
    original = os.environ.pop("WALDO_SKIP_ENVELOPE", None)
    yield
    if original is not None:
        os.environ["WALDO_SKIP_ENVELOPE"] = original
    else:
        os.environ["WALDO_SKIP_ENVELOPE"] = "1"
