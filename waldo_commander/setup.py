"""Named setup storage for panels and standalone Python programs.

``load_setup`` returns an immutable snapshot. Later saves affect subsequent
loads, never a snapshot already held by a running program.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from pprint import pformat

from waldoctl.setup import SetupSnapshot, validate_name

_directory: ContextVar[Path | None] = ContextVar("waldo_setup_directory", default=None)


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
    def __init__(self, directory: str | Path | None = None) -> None:
        selected = directory if directory is not None else _directory.get()
        if selected is None:
            selected = (
                os.environ.get("WALDO_SETUP_DIR")
                or Path.home() / ".waldo-commander" / "setups"
            )
        self.directory = Path(selected).expanduser().resolve()

    def _path(self, name: str) -> Path:
        return self.directory / f"{validate_name(name)}.json"

    def names(self) -> list[str]:
        if not self.directory.exists():
            return []
        names = []
        for path in self.directory.glob("*.json"):
            try:
                names.append(validate_name(path.stem))
            except ValueError:
                continue
        return sorted(names)

    def load(self, name: str) -> SetupSnapshot:
        return SetupSnapshot.from_dict(
            json.loads(self._path(name).read_text(encoding="utf-8"))
        )

    def save(self, name: str, snapshot: SetupSnapshot) -> None:
        destination = self._path(name)
        document = json.dumps(snapshot.to_dict(), indent=2, allow_nan=False) + "\n"
        self.directory.mkdir(parents=True, exist_ok=True)
        # Readers see either complete revision, including during subprocess loads.
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{name}-", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
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
