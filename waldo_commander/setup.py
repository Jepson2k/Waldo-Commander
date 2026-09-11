"""Named setup storage for panels and standalone Python programs.

A named setup is a Python module in ``programs/setups/`` holding one
``SetupSnapshot`` literal, written by the Setup panel and imported by
``load_setup`` (or by the program itself: ``from setups.bench import setup``).
``load_setup`` returns an immutable snapshot. Later saves affect subsequent
loads, never a snapshot already held by a running program.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterator, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from pprint import pformat

from waldoctl.setup import SetupSnapshot, validate_name

from waldo_commander.constants import default_program_dir

_directory: ContextVar[Path | None] = ContextVar("waldo_setup_directory", default=None)
_load_observer: ContextVar[Callable[[str, SetupSnapshot], None] | None] = ContextVar(
    "waldo_setup_load_observer", default=None
)


@contextmanager
def observe_setup_loads(
    observer: Callable[[str, SetupSnapshot], None],
) -> Iterator[None]:
    """Observe the snapshots this program actually loads, without scanning storage."""
    token = _load_observer.set(observer)
    try:
        yield
    finally:
        _load_observer.reset(token)


@contextmanager
def using_setup_directory(directory: str | Path | None) -> Iterator[None]:
    """Select the caller's setup directory inside an isolated preview worker."""
    token = _directory.set(
        Path(directory).expanduser().resolve() if directory is not None else None
    )
    try:
        yield
    finally:
        _directory.reset(token)


class SetupStore:
    """Named setups as Python modules in `programs/setups/`.

    Teaching in the Setup panel writes `setups/<name>.py` — ordinary Python
    holding one `SetupSnapshot` literal — the same way the recorder writes
    programs, so a setup is copied, diffed and versioned like the program
    that uses it, and a program may simply `from setups.bench import setup`.
    `load_setup(name)` imports that module. Setups saved as JSON by earlier
    releases are converted to modules on first use.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        selected = directory if directory is not None else _directory.get()
        if selected is None:
            selected = os.environ.get("WALDO_SETUP_DIR") or (
                default_program_dir() / "setups"
            )
        self.directory = Path(selected).expanduser().resolve()
        self._convert_legacy_json(self.directory)
        if directory is None and _directory.get() is None:
            self._convert_legacy_json(_legacy_home_dir())

    def _path(self, name: str) -> Path:
        return self.directory / f"{validate_name(name)}.py"

    def names(self) -> list[str]:
        if not self.directory.exists():
            return []
        names = []
        for path in self.directory.glob("*.py"):
            try:
                names.append(validate_name(path.stem))
            except ValueError:
                continue
        return sorted(names)

    def load(self, name: str) -> SetupSnapshot:
        snapshot = _import_snapshot(self._path(name))
        observer = _load_observer.get()
        if observer is not None:
            try:
                observer(name, snapshot)
            except Exception:
                logging.getLogger(__name__).exception("Setup recording observer failed")
        return snapshot

    def save(self, name: str, snapshot: SetupSnapshot) -> None:
        destination = self._path(name)
        module = (
            f'"""Named setup {name!r}, written by the Setup panel; edit freely."""\n\n'
            + export_snapshot(snapshot)
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        write_atomic(destination, module)

    def _convert_legacy_json(self, root: Path) -> None:
        """A `.json` setup from an earlier release, here or in the old
        `~/.waldo-commander/setups` store, becomes a module in this directory;
        the JSON goes once the module is on disk, so deleting the setup later
        does not resurrect it."""
        if not root.exists():
            return
        for legacy in root.glob("*.json"):
            try:
                name = validate_name(legacy.stem)
            except ValueError:
                continue
            if self._path(name).exists():
                continue
            try:
                snapshot = SetupSnapshot.from_dict(
                    json.loads(legacy.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, TypeError, KeyError) as error:
                logging.getLogger(__name__).warning(
                    "Legacy setup %s not converted: %s", legacy.name, error
                )
                continue
            self.save(name, snapshot)
            legacy.unlink()


def _legacy_home_dir() -> Path:
    return Path.home() / ".waldo-commander" / "setups"


def _import_snapshot(path: Path) -> SetupSnapshot:
    """Execute a setup module in its own namespace and take its `setup`.

    Compiled from source each time: the import system's bytecode cache is
    keyed on the source's mtime and size, so a re-teach within the same
    second could read back the previous values.
    """
    if not path.is_file():
        raise FileNotFoundError(f"No setup module {path}")
    namespace: dict[str, object] = {
        "__name__": f"setups.{path.stem}",
        "__file__": str(path),
    }
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    except Exception as error:
        raise ValueError(
            f"Setup module {path.name} failed to import: {error}"
        ) from error
    snapshot = namespace.get("setup")
    if not isinstance(snapshot, SetupSnapshot):
        raise ValueError(
            f"Setup module {path.name} must define `setup = SetupSnapshot(...)`"
        )
    return snapshot


def write_atomic(path: Path, text: str) -> None:
    """Readers see either complete revision, including a preview subprocess
    importing the module mid-save."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_setup(name: str, *, directory: str | Path | None = None) -> SetupSnapshot:
    return SetupStore(directory).load(name)


def export_snapshot(snapshot: SetupSnapshot, *, variable: str = "setup") -> str:
    """Return ordinary Python containing the fixed values, without storage I/O."""
    import keyword

    if not variable.isidentifier() or keyword.iskeyword(variable):
        raise ValueError("Snapshot variable must be a Python identifier")
    return f"from waldoctl.setup import SetupSnapshot\n\n{variable} = SetupSnapshot.from_dict({pformat(snapshot.to_dict(), sort_dicts=True)})\n"
