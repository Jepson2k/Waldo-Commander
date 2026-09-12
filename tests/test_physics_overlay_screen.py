"""Browser-level check that a simulated run actually paints.

The achieved path, the contact arrows and the centre-of-mass marker are
three.js geometry: whether they exist in Python says nothing about
whether anything reaches the canvas. This drives the real render path
with a record shaped exactly as a backend produces one and asks the
scene graph what it ended up holding.

It also produces the screenshot for these overlays. The installed
backend here plans and does not simulate, so nothing in the app can
populate a record on its own; the record is injected, and everything
downstream of it is the shipping code.
"""

import time

import numpy as np
import pytest
import waldoctl

from tests.helpers.browser_helpers import click_tab, dismiss_dialogs, run_in_app

# Walks the three.js scene for the overlay group and reports what is in
# it: the achieved polyline (vertex-coloured, so the divergence gradient
# is real geometry and not a uniform), the contact arrows and the COM.
_OVERLAY_JS = """
// The scene lives on its Vue component; NiceGUI addresses those by the
// element id its canvas carries.
const canvas = document.querySelector('canvas');
const host = canvas && canvas.closest('[id^="c"]');
const comp = host && getElement(host.id.slice(1));
const scene = comp && comp.scene;
if (!scene) return null;
let group = null;
scene.traverse((o) => { if (o.name === 'simulation:physics') group = o; });
if (!group) return {found: false};
let lines = 0, meshes = 0, shown = 0, vertexColored = 0, points = 0;
group.traverse((o) => {
  if (o === group) return;
  if (o.isLine || o.isLineSegments) {
    lines += 1;
    if (o.geometry && o.geometry.getAttribute('color')) {
      vertexColored += 1;
      points = Math.max(points, o.geometry.getAttribute('position').count);
    }
  } else if (o.isMesh) {
    meshes += 1;
    if (o.visible) shown += 1;
  }
});
return {found: true, lines, meshes, shown, vertexColored, points};
"""


def _record(q_rad: np.ndarray, rows: int = 40) -> waldoctl.TickIndex:
    """A run the way a backend reports one: a TCP path that sweeps
    forward, a lag that grows along it, and contact for the middle third.

    The joints hold at *q_rad* on every row on purpose. Playback teleports
    the simulated arm to whatever the record says, so a record that moved
    it would leave it moved for every test after this one — and a pose the
    planner will not accept turns into a self-collision refusal three
    tests later. What this checks is the drawing, not the arm.
    """
    t = np.linspace(0.0, 1.0, rows, dtype=np.float32)
    joints = np.tile(np.asarray(q_rad, dtype=np.float32)[:6], (rows, 1))
    commanded = joints + t[:, None] * 0.02
    tcp = np.zeros((rows, 6), dtype=np.float32)
    tcp[:, 0] = 0.25 + t * 0.12
    tcp[:, 2] = 0.30 - t * 0.06

    lo, hi = rows // 3, 2 * rows // 3
    starts = np.zeros(rows + 1, dtype=np.uint32)
    pos, force = [], []
    for r in range(rows):
        if lo <= r < hi:
            pos.append([tcp[r, 0], 0.02, tcp[r, 2] - 0.03])
            force.append([0.0, -6.0, 14.0])
        starts[r + 1] = len(pos)
    com = np.zeros((rows, 3), dtype=np.float32)
    com[:, 0] = 0.10 + t * 0.02
    com[:, 2] = 0.22

    return waldoctl.TickIndex(
        row_dt_s=0.02,
        joints_rad=joints.astype(np.float32),
        commanded_rad=commanded.astype(np.float32),
        tcp=tcp,
        tool_closed=np.linspace(0.0, 1.0, rows, dtype=np.float32),
        tool_gripping=np.zeros(rows, dtype=np.bool_),
        blocks=(waldoctl.TickBlock(command=0, start_row=0, rows=rows, line_number=1),),
        objects=(),
        digest=b"screen-record",
        channels={
            "com": com,
            "contact_pos": np.asarray(pos, dtype=np.float32).reshape(-1, 3),
            "contact_force": np.asarray(force, dtype=np.float32).reshape(-1, 3),
            "contact_starts": starts,
        },
    )


@pytest.mark.browser
def test_a_simulated_run_paints_its_path_contacts_and_com(screen) -> None:
    screen.open("/")
    screen.selenium.set_window_size(1280, 900)
    dismiss_dialogs(screen)
    # The scrub bar and its playback controls live on the program tab,
    # and seeking is what drives the per-frame annotations.
    click_tab(screen, "program")

    def _populate() -> waldoctl.TickIndex:
        view = waldoctl.commander.settings.view
        _saved_view.update({f: getattr(view, f) for f in _VIEW_FLAGS})
        view.paths_visible = True
        view.divergence_visible = True
        view.contacts_visible = True
        view.com_visible = True
        program = waldoctl.commander.programs.active
        assert program is not None
        # A physics pass always follows a planner pass, so a record on
        # screen always has planned segments beside it.
        from waldo_commander.state import PathSegment, simulation_state

        ticks = _record(waldoctl.commander.status.joints.angles.rad)

        program.dry_run.path_segments = [
            PathSegment(
                points=[[float(r[0]), float(r[1]), float(r[2])] for r in ticks.tcp],
                color="#2196f3",
                is_valid=True,
                line_number=1,
                estimated_duration=ticks.duration_s,
            )
        ]
        program.dry_run.total_steps = 1
        program.dry_run.ticks = ticks
        simulation_state.notify_changed()
        return ticks

    ticks = run_in_app(_populate)

    try:
        _check_and_shoot(screen, ticks)
    finally:
        run_in_app(_restore)


_VIEW_FLAGS = (
    "paths_visible",
    "divergence_visible",
    "contacts_visible",
    "com_visible",
)
_saved_view: dict[str, bool] = {}


def _restore() -> None:
    """Put the app back: no record, no segments, no timeline, and the
    view settings as they were.

    A record left behind would be replayed by the next test that touches
    playback, against a program that is not this one; the view flags are
    process-wide and would follow every later test in the session.
    """
    from waldo_commander.components.playback import playback
    from waldo_commander.state import simulation_state

    view = waldoctl.commander.settings.view
    for flag, value in _saved_view.items():
        setattr(view, flag, value)
    _saved_view.clear()

    program = waldoctl.commander.programs.active
    if program is not None:
        program.dry_run.ticks = None
        program.dry_run.path_segments = []
        program.dry_run.total_steps = 0
        program.dry_run.playback.playback_time = 0.0
    playback.invalidate_timeline()
    simulation_state.notify_changed()


def _check_and_shoot(screen, ticks: waldoctl.TickIndex) -> None:
    deadline = time.time() + 15.0
    info = None
    while time.time() < deadline:
        info = screen.selenium.execute_script(_OVERLAY_JS)
        if info and info.get("found") and info.get("vertexColored"):
            break
        time.sleep(0.25)

    def _seek() -> None:
        from waldo_commander.components.playback import playback

        # The frame annotations ride the playback batch, so put playback
        # inside the contact window through the real seek path.
        playback.invalidate_timeline()
        playback.update_scrub_segments()
        assert playback._ensure_timeline() is not None, "no timeline to seek in"
        playback._apply_time(ticks.duration_s * 0.5)

    run_in_app(_seek)

    deadline = time.time() + 10.0
    while time.time() < deadline:
        info = screen.selenium.execute_script(_OVERLAY_JS)
        if info and info.get("shown", 0) >= 2:
            break
        time.sleep(0.25)

    assert info is not None, "no three.js scene on the page"
    assert info.get("found"), "the physics overlay group never reached the scene"
    assert info["vertexColored"] >= 1, (
        f"the achieved path must carry per-vertex colours — a uniform colour "
        f"shows no divergence at all: {info}"
    )
    assert info["points"] >= 40, f"the path is missing rows: {info}"
    assert info["meshes"] >= 2, f"the per-frame pool was never created: {info}"
    assert info["shown"] >= 2, (
        f"expected contact arrows and the centre-of-mass marker to be "
        f"shown at a frame that has contacts: {info}"
    )
    screen.shot("physics-overlay", failed=False)
