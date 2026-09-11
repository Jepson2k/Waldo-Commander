"""Explicit access to files in the current portable project."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from collections.abc import Iterator

from waldo_commander.services.portable_projects import (
    MANIFEST,
    read_manifest,
    safe_path,
)

_current_root: ContextVar[Path | None] = ContextVar("waldo_project_root", default=None)


def find_project(program_path: str | Path | None) -> Path | None:
    """Find a saved program's project; discovery does not execute or apply data."""
    if program_path is None:
        return None
    path = Path(program_path).expanduser().resolve()
    for parent in path.parents:
        manifest = parent / MANIFEST
        if manifest.is_file():
            with manifest.open("rb") as stream:
                data = stream.read(256 * 1024 + 1)
            if len(data) > 256 * 1024:
                raise ValueError("Project manifest is too large")
            read_manifest(data)
            return parent
    return None


def project_file(name: str, *, root: str | Path | None = None) -> Path:
    """Resolve a project resource; standalone callers can supply ``root`` explicitly."""
    selected = root or _current_root.get() or os.environ.get("WALDO_PROJECT_ROOT")
    if not selected:
        raise ValueError(
            "No portable project selected; supply root= for standalone use"
        )
    directory = Path(selected).expanduser().resolve()
    result = (directory / safe_path(name)).resolve()
    if not result.is_relative_to(directory):
        raise ValueError("Project resource resolves outside its folder")
    return result


@contextmanager
def isolated_project(
    root: str | Path | None, program_path: str | None = None
) -> Iterator[None]:
    """Set resource/import paths inside an isolated preview process, then restore them."""
    directory = Path(root).resolve() if root else None
    token = _current_root.set(directory)
    previous_path = list(sys.path)
    previous_cwd = Path.cwd()
    # Preview workers are reused, so a helper this program imports would
    # otherwise stay cached for the next preview — of edited code, or of
    # another project's module by the same name.
    if directory:
        _evict_modules_under(directory)
    loaded_before = set(sys.modules)
    try:
        if directory:
            sys.path.insert(0, str(directory / "programs"))
            if program_path:
                sys.path.insert(0, str(Path(program_path).resolve().parent))
            os.chdir(directory)
        yield
    finally:
        os.chdir(previous_cwd)
        sys.path[:] = previous_path
        _current_root.reset(token)
        for name in set(sys.modules) - loaded_before:
            sys.modules.pop(name, None)


def _evict_modules_under(directory: Path) -> None:
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if file and Path(file).resolve().is_relative_to(directory):
            del sys.modules[name]
