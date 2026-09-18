"""The predicted pass beside the commanded one, from the program's side:
what it draws and enables, what it never locks, and when it is not run."""

import asyncio

import numpy as np
import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_for_urdf_ready,
    wait_until,
)

_SCRIPT = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
rbt.move_j([90, -90, 180, 0, 0, 180], speed=1.0)
"""


def _pair(q_rad, rows: int = 30) -> tuple[waldoctl.TickIndex, waldoctl.TickIndex]:
    """A commanded record and a predicted one that lags it."""
    t = np.linspace(0.0, 1.0, rows, dtype=np.float32)
    joints = np.tile(np.asarray(q_rad, dtype=np.float32)[:6], (rows, 1))
    tcp = np.zeros((rows, 6), dtype=np.float32)
    tcp[:, 0] = 0.25 + t * 0.1
    tcp[:, 2] = 0.3
    blocks = (waldoctl.TickBlock(command=0, start_row=0, rows=rows, line_number=1),)

    def record(q: np.ndarray, digest: bytes) -> waldoctl.TickIndex:
        return waldoctl.TickIndex(
            row_dt_s=0.02,
            joints_rad=q,
            tcp=tcp,
            tool_closed=np.zeros(rows, dtype=np.float32),
            tool_gripping=np.zeros(rows, dtype=np.bool_),
            blocks=blocks,
            digest=digest,
        )

    return record(joints + t[:, None] * 0.02, b"commanded"), record(
        joints, b"predicted"
    )


@pytest.mark.integration
async def test_predicted_overlay_draws_only_when_revisions_match(user: User) -> None:
    """A predicted record is drawn only against the plan it answers. One
    that answers an older revision is not on screen at all — not in the
    scene, not in the layer toggles, not in the legend — and one that is
    the plan itself has nothing to add."""
    from waldo_commander.components.physics_legend import physics_legend
    from waldo_commander.components.playback import layers_available
    from waldo_commander.services.preview_segments import segments_from_record
    from waldo_commander.state import simulation_state, ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    assert scene is not None
    program = waldoctl.commander.programs.active
    assert program is not None
    view = waldoctl.commander.settings.view
    saved = (view.paths_visible, view.predicted_visible)
    view.paths_visible = True
    view.predicted_visible = True
    commanded, predicted = _pair(waldoctl.commander.status.joints.angles.rad)
    overlay = scene.physics_overlay
    assert physics_legend._root is not None
    legend = physics_legend._root
    dry_run = program.dry_run
    try:
        dry_run.commanded = commanded
        dry_run.commanded_revision = 2
        dry_run.path_segments = segments_from_record(commanded, [])
        dry_run.predicted = predicted
        dry_run.predicted_revision = 1
        simulation_state.notify_changed()
        await asyncio.sleep(0)
        assert dry_run.predicted_current is None
        assert not overlay.is_built, "an answer to an older plan must not be drawn"
        assert not layers_available(dry_run)["predicted_visible"]
        assert not legend.visible

        dry_run.predicted_revision = 2
        simulation_state.notify_changed()
        assert await wait_until(lambda: overlay.is_built)
        assert dry_run.predicted_current is predicted
        assert layers_available(dry_run)["predicted_visible"]
        assert legend.visible

        dry_run.predicted = commanded
        simulation_state.notify_changed()
        assert await wait_until(lambda: not overlay.is_built), (
            "a prediction that is the plan draws nothing the plan does not"
        )
        assert not legend.visible
    finally:
        dry_run.commanded = None
        dry_run.commanded_revision = -1
        dry_run.predicted = None
        dry_run.predicted_revision = -1
        dry_run.path_segments = []
        view.paths_visible, view.predicted_visible = saved
        simulation_state.notify_changed()


@pytest.mark.integration
async def test_scrub_bar_never_locks_on_a_planner_only_backend(user: User) -> None:
    """A predicted pass in flight never disables scrubbing: the bar plays
    the commanded record and only shows that a pass is running. On parol6
    the pass comes back as the plan, so every layer toggle stays off, the
    legend stays hidden, and the session does not ask that backend again.
    Driven through the editor's own debounced planning and predicting.
    """
    from waldo_commander.components.physics_legend import physics_legend
    from waldo_commander.components.playback import playback
    from waldo_commander.components.simulation_engine import simulation
    from waldo_commander.services.path_visualizer import path_visualizer
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    program = waldoctl.commander.programs.active
    assert program is not None and ui_state.active_textarea is not None
    dry_run = program.dry_run
    simulation._debounce_delay = 0.2
    simulation._physics_delay = 0.2

    def edit(source: str) -> None:
        textarea = ui_state.active_textarea
        assert textarea is not None
        textarea.value = source
        program.source = source
        # The editor schedules from its change handler, inside its client's
        # slot; the debounce timer needs that client.
        with textarea:
            simulation.schedule_debounced_simulation(program.id)

    edit(_SCRIPT)
    assert await wait_until(lambda: dry_run.commanded is not None, timeout_s=30)
    first = dry_run.commanded
    assert first is not None and dry_run.predicted_current is None
    assert playback._ensure_timeline() is not None

    assert await wait_until(
        lambda: path_visualizer.physics_in_flight(program.id), timeout_s=20
    ), "the predicted pass never started"
    busy = playback._physics_busy
    slider = playback._scrub_slider
    assert busy is not None and busy.visible, "the bar must say a pass is running"
    assert slider is not None and slider.enabled, "scrubbing never waits on a pass"
    assert playback.play_btn is not None and playback.play_btn.enabled

    assert await wait_until(
        lambda: not path_visualizer.physics_in_flight(program.id), timeout_s=60
    )
    assert await wait_until(lambda: not busy.visible, timeout_s=5)
    assert path_visualizer._predicted_diverges["parol6"] is False
    assert not any(box.enabled for box in playback._layer_checks.values())
    assert physics_legend._root is not None and not physics_legend._root.visible

    # The next plan is not answered: the probe is not repeated this session.
    edit(_SCRIPT.replace("180]", "170]"))
    assert await wait_until(
        lambda: dry_run.commanded is not None and dry_run.commanded is not first,
        timeout_s=30,
    )
    assert dry_run.predicted_current is None, "a new plan retires the answer"
    assert await wait_until(
        lambda: simulation._simulation_debounce_timer is None
        and simulation._physics_timer is None,
        timeout_s=20,
    )
    assert dry_run.predicted is None
    assert not path_visualizer.physics_in_flight(program.id)
