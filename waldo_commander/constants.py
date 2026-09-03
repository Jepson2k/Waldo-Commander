import logging
import os
from pathlib import Path
from typing import Any

# Repository root
REPO_ROOT = Path(__file__).resolve().parent.parent


def default_program_dir() -> Path:
    """The on-disk program library (example + saved programs)."""
    legacy = REPO_ROOT / "PAROL-commander-software" / "GUI" / "files" / "Programs"
    if legacy.exists():
        return legacy
    d = REPO_ROOT / "programs"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _Config:
    """Lazy configuration that reads environment variables at access time.

    This allows tests to set environment variables after module import
    and have them take effect without module cache manipulation.

    Runtime overrides (e.g., from CLI arguments) take precedence over env vars.
    Use config.set('property_name', value) to set overrides.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}

    def set(self, key: str, value: object) -> None:
        """Set a runtime override (e.g., from CLI arguments).

        Args:
            key: Property name (e.g., 'server_port', 'controller_host')
            value: Override value
        """
        self._overrides[key] = value

    @property
    def controller_host(self) -> str:
        """Controller target host (what the UI connects to)."""
        if "controller_host" in self._overrides:
            return str(self._overrides["controller_host"])
        return os.getenv("WALDO_CONTROLLER_IP", "127.0.0.1")

    @property
    def controller_port(self) -> int:
        """Controller target UDP port."""
        if "controller_port" in self._overrides:
            return int(self._overrides["controller_port"])  # type: ignore[call-overload]
        return int(os.getenv("WALDO_CONTROLLER_PORT", "5001"))

    @property
    def exclusive_start(self) -> bool:
        """Whether to require exclusive controller ownership on start."""
        if "exclusive_start" in self._overrides:
            return bool(self._overrides["exclusive_start"])
        return os.getenv("WALDO_EXCLUSIVE_START", "1").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    @property
    def server_host(self) -> str:
        """Webserver bind host (NiceGUI)."""
        if "server_host" in self._overrides:
            return str(self._overrides["server_host"])
        return os.getenv("WALDO_SERVER_IP", "0.0.0.0")

    @property
    def server_port(self) -> int:
        """Webserver bind port (NiceGUI)."""
        if "server_port" in self._overrides:
            return int(self._overrides["server_port"])  # type: ignore[call-overload]
        return int(os.getenv("WALDO_SERVER_PORT", "8080"))

    @property
    def log_level(self) -> int:
        """Logging level."""
        if "log_level" in self._overrides:
            return int(self._overrides["log_level"])  # type: ignore[call-overload]
        s = os.getenv("WALDO_LOG_LEVEL")
        if s:
            name = s.strip().upper()
            mapping = {
                "DEBUG": logging.DEBUG,
                "INFO": logging.INFO,
                "WARNING": logging.WARNING,
                "ERROR": logging.ERROR,
                "CRITICAL": logging.CRITICAL,
            }
            return mapping.get(name, logging.WARNING)
        return logging.WARNING

    @property
    def webapp_control_rate_hz(self) -> float:
        """Webapp control emission rate in Hz."""
        if "webapp_control_rate_hz" in self._overrides:
            return float(self._overrides["webapp_control_rate_hz"])  # type: ignore[arg-type]
        return float(os.getenv("WALDO_WEBAPP_CONTROL_RATE_HZ", "20"))

    @property
    def webapp_control_interval_s(self) -> float:
        """Webapp control emission interval in seconds."""
        return 1.0 / max(self.webapp_control_rate_hz, 1.0)


# Default 3D scene camera position
DEFAULT_CAMERA = dict(x=0.3, y=0.3, z=0.22, look_at_z=0.22)

# Gripper camera feed resolution
CAMERA_FEED_W = 640
CAMERA_FEED_H = 480


# Waypoint marker sizes (meters) for 3D path visualization
WAYPOINT_SIZE_LARGE = 0.008  # Editable targets (with TransformControls)
WAYPOINT_SIZE_SMALL = 0.004  # Non-editable segment endpoints

# Click vs hold threshold for jog buttons and keyboard shortcuts
CLICK_HOLD_THRESHOLD_S: float = 0.15

# Calibration is unchecked firmware motion, so the HOME hold must be
# unmistakably deliberate.
HOME_LONG_PRESS_S: float = 0.8

# Core panel tab ids a plugin panel may never claim. Lives here (not main.py)
# so components can import it without importing main — importing main from a
# component re-executes it under screen tests (main runs via runpy there) and
# re-registers "/" with a handler whose panel globals were never initialized.
RESERVED_TAB_IDS = frozenset({"program", "io", "gripper", "response", "log", "help"})

config = _Config()
