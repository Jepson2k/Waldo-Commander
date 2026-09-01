import asyncio
import logging
import os
import sys
import threading
import weakref
from typing import Any

from nicegui import ui, Client

_LEVEL_COLORS = {
    "TRACE": "\033[32m",  # green
    "DEBUG": "\033[36m",  # cyan
    "INFO": "\033[37m",  # light gray
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
    "CRITICAL": "\033[41m",  # red background
}

# Mirrors CLI color semantics for NiceGUI log lines.
_LEVEL_CLASSES = {
    "TRACE": "log-trace",
    "DEBUG": "log-debug",
    "INFO": "log-info",
    "WARNING": "log-warning",
    "ERROR": "log-error",
    "CRITICAL": "log-critical",
}

_RESET = "\033[0m"
_DIM = "\033[2m"

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


class TraceLogger(logging.Logger):
    """Logger subclass with a ``trace()`` method for sub-DEBUG verbosity."""

    def trace(self, msg: object, *args: object, **kwargs: Any) -> None:
        if self.isEnabledFor(TRACE):
            self._log(TRACE, msg, args, **kwargs)


# Install trace on the base Logger class so *all* loggers support it
if not hasattr(logging.Logger, "trace"):
    logging.Logger.trace = TraceLogger.trace  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    logging.TRACE = TRACE  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

# Environment guard to make hot-path trace zero-cost unless explicitly enabled
TRACE_ENABLED = str(os.getenv("WALDO_TRACE", "0")).lower() in ("1", "true", "yes", "on")


class AnsiColorFormatter(logging.Formatter):
    """Formatter that adds ANSI colors and a compact timestamp."""

    def __init__(self, colored: bool = True) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
        )
        self.colored = colored and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if not self.colored:
            return base
        level = record.levelname.upper()
        color = _LEVEL_COLORS.get(level, "")
        # Expect format "HH:MM:SS LEVEL logger: msg"
        try:
            ts, rest = base.split(" ", 1)
            if color:
                rest = rest.replace(level, f"{color}{level}{_RESET}", 1)
            return f"{_DIM}{ts}{_RESET} {rest}"
        except Exception:
            return base


# ---- NiceGUI UI log handler ----

_ui_log_targets: set[weakref.ref] = set()
_ui_lock = threading.Lock()


class NiceGuiLogHandler(logging.Handler):
    """Push log records into one or more NiceGUI ui.log widgets."""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        if not _ui_log_targets:
            return
        # NiceGUI's own records must never reach a NiceGUI widget: pushing
        # to the log element enqueues an outbox message, and when the
        # outbox itself is what errored (e.g. its AppConfig lookup during
        # test teardown), each push triggers the next error — a hot
        # feedback loop that spins until something kills the process.
        if record.name == "nicegui" or record.name.startswith("nicegui."):
            return
        msg = self.format(record)
        level = record.levelname.upper()
        classes = _LEVEL_CLASSES.get(level, "log-info")
        # UI elements belong to the app's event loop. A push from another
        # thread (the controller output reader, an executor) mutates
        # client.elements while the loop may be iterating it — page
        # refreshes crash with "dictionary changed size during iteration".
        # Marshal off-loop records onto the loop; drop them when no loop
        # is running (shutdown, early startup).
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            from nicegui import core

            loop = getattr(core, "loop", None)
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._push_to_targets, msg, classes)
            return
        self._push_to_targets(msg, classes)

    @staticmethod
    def _push_to_targets(msg: str, classes: str) -> None:
        stale: list[weakref.ref] = []
        with _ui_lock:
            for ref in list(_ui_log_targets):
                widget = ref()
                if widget is None:
                    stale.append(ref)
                    continue
                # Skip dead clients to avoid NiceGUI's "Client has been deleted" warning.
                try:
                    client_ref = widget._client()
                    if client_ref is None or client_ref.id not in Client.instances:
                        stale.append(ref)
                        continue
                except (RuntimeError, AttributeError):
                    stale.append(ref)
                    continue
                try:
                    widget.push(msg, classes=classes)
                except Exception:
                    stale.append(ref)
            for ref in stale:
                _ui_log_targets.discard(ref)


def attach_ui_log(log_widget) -> None:
    """Register a ui.log widget as a sink for log records."""
    if ui is None:
        return
    try:
        ref = weakref.ref(log_widget)
    except TypeError:
        return
    with _ui_lock:
        _ui_log_targets.add(ref)


def _have_console_handler(logger: logging.Logger) -> bool:
    return any(isinstance(h, logging.StreamHandler) for h in logger.handlers)


def _have_ui_handler(logger: logging.Logger) -> bool:
    return any(isinstance(h, NiceGuiLogHandler) for h in logger.handlers)


def configure_logging(
    level: int = logging.INFO, use_color: bool = True, add_ui_handler: bool = True
) -> logging.Logger:
    """
    Configure root logger with:
      - ANSI-colored console handler (stderr) with timestamps and levels
      - Optional NiceGUI UI log handler (messages mirrored to web log)
    Idempotent across multiple calls.
    """
    logger = logging.getLogger()
    logger.setLevel(level)

    if not _have_console_handler(logger):
        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(level)
        console.setFormatter(AnsiColorFormatter(colored=use_color))
        logger.addHandler(console)

    if add_ui_handler and not _have_ui_handler(logger):
        ui_handler = NiceGuiLogHandler(level=level)
        logger.addHandler(ui_handler)

    # Silence verbose third-party loggers unconditionally. Log lines are
    # forwarded to the webpage (NiceGuiLogHandler), so third-party DEBUG
    # floods (e.g. toppra's per-sample forward-pass lines) drastically slow
    # the UI and tests. Debugging one of these libraries requires a manual
    # setLevel override.
    quiet_level = max(level, logging.WARNING)
    for name in (
        "numba",
        "numba.core",
        "numba.cuda",
        "toppra",
        "uvicorn.access",
        "nicegui",
    ):
        logging.getLogger(name).setLevel(quiet_level)

    return logger
