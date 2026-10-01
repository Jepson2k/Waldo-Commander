"""Reading what a stepped program publishes to its GUI controller."""

from __future__ import annotations

import time


def _drain(controller, count, timeout=2.0):
    """Events the program published, waiting for `count` of them."""
    deadline = time.monotonic() + timeout
    events = []
    while len(events) < count and time.monotonic() < deadline:
        events.extend(controller.poll_events())
        time.sleep(0.01)
    return events
