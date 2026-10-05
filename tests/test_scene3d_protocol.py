"""What the 3D view sends the browser: nothing until it has mounted, then the
whole scene, then what changed in each pass of the event loop as one message."""

from __future__ import annotations

import asyncio
import json
import math
import threading
from typing import Any

import pytest
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from tests.helpers.wait import wait_for_app_ready, wait_for_urdf_ready
from waldo_commander.common.theme import hex_of
from waldo_commander.scene3d import WcScene


def _ops(code: str) -> list[list[Any]]:
    head = ",'apply',["
    return json.loads(code[code.index(head) + len(head) : -2])


@pytest.mark.integration
async def test_the_scene_sends_what_changed_once_per_pass(user: User) -> None:
    await user.open("/")
    await wait_for_app_ready()
    await wait_for_urdf_ready()
    assert user.client is not None
    with user.client.content:
        scene = WcScene(background=hex_of("scene-bg"), reach=0.5)
    sent: list[list[list[Any]]] = []
    run_javascript = scene.client.run_javascript
    own = f"runMethod({scene.id},'apply',"

    def capture(code: str, *args: Any, **kwargs: Any) -> Any:
        if own not in code:
            return run_javascript(code, *args, **kwargs)
        sent.append(_ops(code))

    scene.client.run_javascript = capture  # type: ignore[method-assign]

    async def one_pass() -> list[list[Any]]:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(sent) <= 1, f"{len(sent)} messages for one pass: {sent}"
        return sent.pop() if sent else []

    # Before the browser has mounted the view, nothing is sent; the mount gets
    # the whole scene, parents before children.
    with scene:
        arm = scene.group().with_name("arm").move(0.1, 0, 0)
        with arm:
            joint = scene.joint([0, 0, 1], "revolute")
            with joint:
                link = scene.box(0.1, 0.2, 0.3).material(hex_of("axis-x"))
        marker = scene.sphere(0.01).with_name("marker")
    scene.define_joints([joint])
    scene.set_joint_values([0.25])
    scene.move_camera(1, 1, 1, duration=0)
    assert await one_pass() == []
    UserInteraction(user, {scene}, None).trigger("init", {})
    snapshot = await one_pass()
    assert snapshot[0] == ["reset"]
    creates = [op for op in snapshot if op[0] == "c"]
    assert [op[1] for op in creates] == [arm.id, joint.id, link.id, marker.id]
    assert [op[2] for op in creates] == [0, arm.id, joint.id, 0]
    assert creates[0][5] == {"n": "arm", "p": [0.1, 0, 0]}
    assert creates[2][4] == [0.1, 0.2, 0.3]
    assert creates[2][5]["m"] == [hex_of("axis-x"), 1.0, "front"]
    assert ["J", [joint.id]] in snapshot and ["q", [0.25]] in snapshot
    assert ["cam", [1, 1, 1, 0, 0, 0], 0.0, False] in snapshot

    # Changes made in one pass go as one message: deletes, then creates with
    # their state, then only the properties that changed, at their last value.
    arm.move(0.2, 0, 0)
    arm.move(0.3, 0, 0)
    link.material(hex_of("axis-y"), 0.5)
    with arm:
        added = scene.cylinder(0, 0.003, 0.006).move(0, 0, 0.1)
    marker.delete()
    gone = scene.box()
    gone.delete()
    scene.set_joint_values([0.5])
    ops = await one_pass()
    assert ops[0] == ["d", marker.id]
    assert ops[1] == [
        "c",
        added.id,
        arm.id,
        "cylinder",
        [0, 0.003, 0.006, 8, 1],
        {"p": [0, 0, 0.1]},
    ]
    assert ["u", arm.id, {"p": [0.3, 0, 0]}] in ops
    assert ["u", link.id, {"m": [hex_of("axis-y"), 0.5, "front"]}] in ops
    assert ["q", [0.5]] in ops
    assert not any(gone.id in op[1:2] for op in ops), (
        "a node made and deleted in one pass was sent"
    )

    # The same value again is no change; a non-finite joint value is dropped.
    arm.move(0.3, 0, 0)
    link.material(hex_of("axis-y"), 0.5)
    scene.set_joint_values([math.nan])
    assert await one_pass() == []

    # An effect on a node deleted before it is sent is dropped; a ripple asked
    # to stop is not.
    scene.fx.alarm([added], hex_of("axis-x"))
    added.delete()
    scene.fx.pulse([])
    ops = await one_pass()
    assert ops == [["d", added.id], ["fx", "pulse", []]]

    # A rotation travels as a quaternion, rounded.
    link.rotate(0, 0, math.pi / 2)
    ((code, nid, state),) = await one_pass()
    assert (code, nid) == ("u", link.id)
    assert state["r"] == pytest.approx([0, 0, math.sqrt(0.5), math.sqrt(0.5)], abs=1e-7)

    # Only the event loop may change the scene.
    failure: list[BaseException] = []

    def off_loop() -> None:
        try:
            link.move(1, 1, 1)
        except RuntimeError as error:
            failure.append(error)

    worker = threading.Thread(target=off_loop)
    worker.start()
    worker.join()
    assert failure, "a change from another thread was accepted"
    assert (link.x, link.y, link.z) == (0, 0, 0)
