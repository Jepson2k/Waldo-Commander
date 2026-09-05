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

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app

# Walks the three.js scene for the overlay group and reports what is in
# it: the achieved polyline (vertex-coloured, so the divergence gradient
# is real geometry and not a uniform), the contact arrows and the COM.
_OVERLAY_JS = """
const scene = window.scene3d || (window.sceneInstances
  && Object.values(window.sceneInstances)[0]
  && Object.values(window.sceneInstances)[0].scene);
if (!scene) return null;
let group = null;
scene.traverse((o) => { if (o.name === 'simulation:physics') group = o; });
if (!group) return {found: false};
let lines = 0, meshes = 0, vertexColored = 0, points = 0;
group.traverse((o) => {
  if (o === group) return;
  if (o.isLine || o.isLineSegments) {
    lines += 1;
    if (o.geometry && o.geometry.getAttribute('color')) {
      vertexColored += 1;
      points = Math.max(points, o.geometry.getAttribute('position').count);
    }
  } else if (o.isMesh) {
    meshes += o.visible ? 1 : 0;
  }
});
return {found: true, lines, meshes, vertexColored, points};
"""


def _record(rows: int = 40) -> waldoctl.TickIndex:
    """A run the way a backend reports one: the arm sweeps forward, its
    lag grows, and something is in contact for the middle third."""
    t = np.linspace(0.0, 1.0, rows, dtype=np.float32)
    commanded = np.tile(t[:, None], (1, 6))
    joints = commanded - t[:, None] * 0.02
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

    def _populate():
        view = waldoctl.commander.settings.view
        view.paths_visible = True
        view.divergence_visible = True
        view.contacts_visible = True
        view.com_visible = True
        program = waldoctl.commander.programs.active
        assert program is not None
        program.dry_run.ticks = _record()
        from waldo_commander.components.playback import playback
        from waldo_commander.state import simulation_state, ui_state

        simulation_state.notify_changed()
        scene = ui_state.urdf_scene
        assert scene is not None
        # The frame annotations ride the playback batch, so put playback
        # somewhere inside the contact window.
        playback._apply_time(program.dry_run.ticks.duration_s * 0.5)
        return scene

    run_in_app(_populate)

    deadline = time.time() + 15.0
    info = None
    while time.time() < deadline:
        info = screen.selenium.execute_script(_OVERLAY_JS)
        if info and info.get("found") and info.get("vertexColored"):
            break
        time.sleep(0.25)

    assert info is not None, "no three.js scene on the page"
    assert info.get("found"), "the physics overlay group never reached the scene"
    assert info["vertexColored"] >= 1, (
        f"the achieved path must carry per-vertex colours — a uniform colour "
        f"shows no divergence at all: {info}"
    )
    assert info["points"] >= 40, f"the path is missing rows: {info}"
    assert info["meshes"] >= 2, (
        f"expected contact arrows and the centre-of-mass marker: {info}"
    )
    screen.shot("physics-overlay")
