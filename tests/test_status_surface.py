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

from tests.helpers.wait import poll_until, wait_for_app_ready, wait_for_urdf_ready

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


@pytest.mark.integration
async def test_a_link_state_that_is_not_an_enum_does_not_stop_the_tick(
    user: User,
) -> None:
    """waldoctl documents ``link_health["state"]`` as a backend enum OR a
    string, and the key as one a backend may not send at all.

    The consumer did ``link["state"].name``: a bare subscript, then an
    attribute only half that contract has. Either way it raised on the
    status tick, inside the per-tick handler that logs at DEBUG -- so the
    loop spun at the full status rate and everything after it, homing
    included, was skipped for as long as the backend stayed connected.
    """
    await user.open("/")
    await wait_for_app_ready()

    # parol6 never fills link_health, so what is staged here survives tick
    # after tick -- the same reason the warnings test stages there.
    shared = waldoctl.commander.client._shared_status

    shared.link_health = {"state": "ACTIVE", "restarts": 3}
    await poll_until(
        lambda: waldoctl.commander.status.link_health.state,
        lambda v: v == "ACTIVE",
        timeout_s=8.0,
        what="a string link state reaching the status surface",
    )
    assert waldoctl.commander.status.link_health.restarts == 3

    # And a backend that reports the block without a state at all.
    shared.link_health = {"restarts": 4}
    await poll_until(
        lambda: waldoctl.commander.status.link_health.restarts,
        lambda v: v == 4,
        timeout_s=8.0,
        what="link health surviving a missing state key",
    )


@pytest.mark.integration
async def test_a_malformed_warning_does_not_take_the_tick_with_it(
    user: User,
) -> None:
    """``RobotError.from_wire`` unpacks exactly six elements, so an entry
    that is not a 6-tuple raises -- on the status tick, inside the per-tick
    handler that logs at DEBUG. One bad entry would take the rest of that
    tick with it every tick, for as long as the condition stood.
    """
    await user.open("/")
    await wait_for_app_ready()

    shared = waldoctl.commander.client._shared_status
    shared.warnings = [(-1, 59, "truncated"), DEGRADED]

    # The well-formed one still arrives, which it cannot do if the bad entry
    # ahead of it aborted the tick.
    await user.should_see(
        kind=Notification, content="Control loop degraded", retries=50
    )
    shared.warnings = []
