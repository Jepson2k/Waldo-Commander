"""Skills use the same native planner as direct Python commands."""

import asyncio
from typing import cast

import numpy as np
import pytest
from parol6.client.dry_run_client import DryRunRobotClient
from waldoctl.client import RobotClient
from waldoctl.skills import SkillError, UnresolvedPreview, skill

from waldo_commander.services.path_preview_client import (
    AsyncPathPreviewClient,
    PathPreviewClient,
)
from waldo_commander.skills import retract


def test_imported_skill_previews_sync_async_and_failed_motion():
    def preview():
        return PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            initial_joints=np.radians([85, -85, 135, 10, 45, 170]),
        )

    client = preview()
    start = np.array(client.pose()[:3])
    index = retract(client, distance_mm=2.0)
    assert index >= 0 and client.wait_command(index)
    assert np.linalg.norm(np.array(client.pose()[:3]) - start) == pytest.approx(
        2.0, abs=0.15
    )
    assert client.segment_collector and all(
        s["is_valid"] for s in client.segment_collector
    )

    other = preview()
    async_client = cast(RobotClient, AsyncPathPreviewClient.from_sync(other))
    asyncio.run(retract.async_call(async_client, distance_mm=2.0))
    assert other.pose() == pytest.approx(client.pose(), abs=0.01)

    with pytest.raises(SkillError, match="rejected"):
        retract(client, distance_mm=10000.0)
    assert any(not s["is_valid"] for s in client.segment_collector)
    assert not client.wait_command(100000), "an unknown command cannot be complete"

    @skill(id="test.observe", version="1.0.0")
    async def observe(rbt: RobotClient) -> bool:
        return await rbt.wait_status(lambda status: bool(status.io[0]))

    with pytest.raises(UnresolvedPreview):
        observe(client)
    with pytest.raises(UnresolvedPreview):
        client.io()


def test_blended_skill_waits_use_planner_results():
    @skill(id="test.blend", version="1.0.0", requires=frozenset({"backend.parol6"}))
    async def blend(rbt: RobotClient) -> bool:
        first = await rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5, r=2.0)
        last = await rbt.move_j([90, -90, 180, 0, 0, 180], speed=0.5, r=0.0)
        assert first >= 0 and last > first
        assert await rbt.wait_command(first), (
            "a dispatched blend includes its first member"
        )
        # A group still buffered when the skill waits must also flush normally.
        last = await rbt.move_j([90, -90, 180, 0, 0, 180], speed=0.5, r=2.0)
        return await rbt.wait_command(last)

    client = PathPreviewClient(dry_run_client_cls=DryRunRobotClient)
    assert blend(client)
    assert client.segment_collector
    assert client.angles() == pytest.approx([90, -90, 180, 0, 0, 180], abs=0.1)
