"""Persisted robot/simulator boot preference.

Same dual-write shape as ``control_lease``'s control mode: the GUI toggle
updates the live flag and persists the human's choice here; startup consults
it before falling back to port-based detection.
"""

from nicegui import app

SIM = "sim"
HARDWARE = "hardware"
_STORAGE_KEY = "startup_mode"


def set_startup_mode(sim_enabled: bool) -> None:
    app.storage.general[_STORAGE_KEY] = SIM if sim_enabled else HARDWARE


def stored_startup_mode() -> str:
    """The persisted preference, or ``""`` when the user never chose."""
    return app.storage.general.get(_STORAGE_KEY, "")
