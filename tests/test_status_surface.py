"""The standing-warning banner, end to end over the status path.

Warning-class conditions self-clear, so the banner tracks the standing set
and nothing else: it has to appear while a condition stands and leave when
the condition does. The durable half — the Diagnostics event log — is
covered in ``test_diagnostics.py``.

Runs on the suite's parol6 fake-serial backend, which has no warning source
of its own, so the condition is staged on the buffer its client fills from
the wire. Everything downstream of that buffer is the app's own path: the
status consumer, ``commander.status.warnings``, and the banner.
"""

import pytest
import waldoctl
from nicegui.elements.notification import Notification
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready, wait_for_urdf_ready

DEGRADED = (
    -1,
    59,
    "Control loop degraded",
    "p99 over band",
    "motion may stutter",
    "reduce background load",
)


@pytest.mark.integration
async def test_the_warning_banner_leaves_with_its_condition(user: User) -> None:
    """The banner is the only thing saying a condition stands right now.

    One that outlives its condition is worse than none: it reports a robot
    state that is no longer true, and nothing else on the page contradicts
    it. The client's dismiss event is what deletes the element, so a page
    that never sends one must still see the banner go.
    """
    await user.open("/")
    await wait_for_app_ready()
    await wait_for_urdf_ready()

    # ``warnings`` is the one StatusBuffer field parol6 never fills, so the
    # decoder leaves whatever is staged here in place tick after tick.
    shared = waldoctl.commander.client._shared_status
    shared.warnings = [DEGRADED]
    await user.should_see(
        kind=Notification, content="Control loop degraded", retries=50
    )
    assert waldoctl.commander.status.warnings.entries, (
        "the condition reached the public status surface"
    )

    shared.warnings = []
    await user.should_not_see(
        kind=Notification, content="Control loop degraded", retries=50
    )
