"""Physics-pool jobs that import nothing of the app: a spawned worker imports
the module its job comes from, so each fresh worker stays cheap to start."""

import os
from pathlib import Path
import time


def blocked_job(paths: tuple[str, str]) -> int:
    """Write this worker's pid to the first path, then wait for the second."""
    marker, release = map(Path, paths)
    marker.write_text(str(os.getpid()))
    while not release.exists():
        time.sleep(0.01)
    return os.getpid()


def pid_job(_args: tuple) -> int:
    return os.getpid()


def dying_job(_args: tuple) -> int:
    os._exit(1)
