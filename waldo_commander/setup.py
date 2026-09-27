"""Named setup storage for panels and standalone Python programs.

``load_setup`` returns an immutable snapshot. Later saves affect subsequent
loads, never a snapshot already held by a running program.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields
from pathlib import Path
from pprint import pformat

from waldoctl.setup import SetupSnapshot, validate_name

logger = logging.getLogger(__name__)

_directory: ContextVar[Path | None] = ContextVar("waldo_setup_directory", default=None)
_save_listeners: list[Callable[[Path, str, str], None]] = []


def add_save_listener(
    listener: Callable[[Path, str, str], None],
) -> Callable[[], None]:
    """Call ``listener(directory, name, revision)`` on every save; returns a remover."""
    _save_listeners.append(listener)

    def remove() -> None:
        with contextlib.suppress(ValueError):
            _save_listeners.remove(listener)

    return remove


def merge_snapshots(
    base: SetupSnapshot, ours: SetupSnapshot, theirs: SetupSnapshot
) -> SetupSnapshot:
    """Per entry, what *ours* changed from *base* wins and the rest is *theirs*."""
    merged: dict[str, dict] = {}
    for section in fields(SetupSnapshot):
        before, mine, other = (
            getattr(snapshot, section.name) for snapshot in (base, ours, theirs)
        )
        entries = dict(other)
        for name in before.keys() | mine.keys():
            if mine.get(name) == before.get(name):
                continue
            if name in mine:
                entries[name] = mine[name]
            else:
                entries.pop(name, None)
        merged[section.name] = entries
    return SetupSnapshot(**merged)


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
        return self.read(name)[0]

    def read(self, name: str) -> tuple[SetupSnapshot, str]:
        """The saved snapshot and its revision, the SHA-256 of the file."""
        data = self._path(name).read_bytes()
        snapshot = SetupSnapshot.from_dict(json.loads(data.decode("utf-8")))
        return snapshot, hashlib.sha256(data).hexdigest()

    def revision(self, name: str) -> str | None:
        """The SHA-256 of the saved file, or None when there is none."""
        try:
            return hashlib.sha256(self._path(name).read_bytes()).hexdigest()
        except FileNotFoundError:
            return None

    def save(self, name: str, snapshot: SetupSnapshot) -> str:
        """Write *snapshot* under *name* and return its new revision."""
        destination = self._path(name)
        # Bytes, not text: a platform newline translation would make the file
        # differ from the revision reported here.
        data = (
            json.dumps(snapshot.to_dict(), indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        self.directory.mkdir(parents=True, exist_ok=True)
        # Readers see either complete revision, including during subprocess loads.
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{name}-", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        revision = hashlib.sha256(data).hexdigest()
        for listener in list(_save_listeners):
            try:
                listener(self.directory, name, revision)
            except Exception:
                logger.exception("Setup save listener failed")
        return revision


def load_setup(name: str, *, directory: str | Path | None = None) -> SetupSnapshot:
    return SetupStore(directory).load(name)


def export_snapshot(snapshot: SetupSnapshot, *, variable: str = "setup") -> str:
    """Return ordinary Python containing the fixed values, without storage I/O."""
    import keyword

    if not variable.isidentifier() or keyword.iskeyword(variable):
        raise ValueError("Snapshot variable must be a Python identifier")
    return f"from waldoctl.setup import SetupSnapshot\n\n{variable} = SetupSnapshot.from_dict({pformat(snapshot.to_dict(), sort_dicts=True)})\n"
