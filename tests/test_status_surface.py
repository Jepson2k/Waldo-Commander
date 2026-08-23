"""The v0.8.0 status surface: controller chip, warnings banner, homing
progress.

These run on the suite's parol6 fake-serial backend, which pre-dates the
v0.8.0 buffer fields — which is itself half the contract under test: the
status consumer must leave the new sub-objects at their defaults instead of
resetting them each tick, so the UI wiring can be driven through the public
``commander.status`` surface exactly the way a capable backend's consumer
writes it. The full wire-to-widget path runs against the real par6 runtime
in ``test_par6_backend.py``.
"""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready


@pytest.mark.integration
async def test_status_surface_renders_from_commander_state(user: User) -> None:
    """Controller chip, warnings banner and homing progress render what the
    status consumer writes to ``commander.status`` — driven through the same
    bindable sub-objects the consumer mutates, asserted by what the page
    shows."""
    await user.open("/")
    await wait_for_app_ready()

    st = waldoctl.commander.status

    st.controller.mode = "JOG"
    await asyncio.sleep(0)
    await user.should_see("JOG")

    st.warnings.entries = [
        (-1, 59, "Control loop degraded", "p99 over band", "warning", "reduce load")
    ]
    await asyncio.sleep(0)
    await user.should_see("Control loop degraded")

    st.homing.sequence_step = 2
    st.homing.joints = [("DONE", "FINISHED"), ("RUNNING", "SEEK")]
    st.homing.active = True
    await asyncio.sleep(0)
    await user.should_see("Homing step 2")
    await user.should_see("J2:RUNNING(SEEK)")

    # Self-clearing: the banner and the progress row leave with their state.
    st.warnings.entries = []
    st.homing.active = False
    await asyncio.sleep(0)
    await user.should_not_see("Control loop degraded")
