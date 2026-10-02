"""Saving a setup includes pending edits across tabs without partial writes."""

import pytest
from nicegui import ui
from waldoctl.camera import CameraCalibration, CameraIntrinsics, CameraQuality
from waldoctl.setup import Frame, Parameter, Pose, SetupSnapshot

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.services.handeye import BoardSpec
from waldo_commander.setup import SetupStore, export_snapshot

# A section only the camera calibration panel writes.
OVERHEAD = CameraCalibration(
    "overhead-camera",
    "parol6",
    "fixed",
    Pose((100, -50, 200, 5, 10, 20)),
    CameraIntrinsics((800, 0, 320, 0, 800, 240, 0, 0, 1), (0, 0, 0, 0, 0), (640, 480)),
    CameraQuality(12, "PARK", 0.2, (0.1, 0.3), (0.2, 0.5), 0.2),
    "2026-09-07T00:00:00Z",
    BoardSpec().to_dict(),
    reference_wrf=Pose((0, 0, 0, 0, 0, 0)),
)


@pytest.mark.integration
async def test_save_pending_edits_without_partial_writes_or_silent_overwrites(
    user, tmp_path, monkeypatch
):
    """Save replaces a saved setup silently only when it is the one the panel
    loaded and nothing has written it since, and never drops a section
    another panel wrote."""
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    store = SetupStore()
    original = SetupSnapshot(
        frames={"fixture": Frame((10, 0, 0, 0, 0, 0))},
        poses={"pick": Pose((1, 2, 3, 0, 0, 0), "fixture")},
    )
    store.save("bench", original)
    await user.open("/")
    await wait_for_app_ready()

    def field(marker):
        return next(iter(user.find(marker=marker).elements))

    user.find(marker="tab-setup").click()

    # The panel starts empty under the name "bench" without having loaded it.
    user.find(marker="setup-save").click()
    await user.should_see(marker="setup-replace-load")
    assert store.load("bench") == original
    user.find(marker="setup-replace-load").click()
    await user.should_see("Loaded bench")
    assert field("setup-frame-x").value == 10

    # Loaded and unchanged since: Save writes straight away, with the pending
    # edit of every tab.
    field("setup-frame-x").set_value(20)
    user.find(kind=ui.tab, content="Poses").click()
    field("setup-pose-z").set_value(8)
    user.find(kind=ui.tab, content="Parameters").click()
    field("setup-parameter-value").set_value("12")
    user.find(marker="setup-save").click()
    await user.should_see("Saved bench")
    saved = store.load("bench")
    assert saved.frames["fixture"].values[0] == 20
    assert saved.resolve("pick").values[:3] == (21, 2, 8)
    assert saved.parameters["clearance"].value == 12

    # One invalid tab refuses the whole save.
    user.find(kind=ui.tab, content="Frames").click()
    field("setup-frame-x").set_value(99)
    user.find(kind=ui.tab, content="Parameters").click()
    field("setup-parameter-value").set_value("invalid")
    user.find(marker="setup-save").click()
    await user.should_see("could not convert string to float")
    assert store.load("bench") == saved
    field("setup-parameter-value").set_value("14")
    user.find(marker="setup-save").click()
    await user.should_see("Saved bench")
    assert store.load("bench").resolve("pick").values[0] == 100

    user.find(kind=ui.tab, content="Frames").click()
    field("setup-frame-x").set_value(15)
    user.find(marker="setup-load").click()
    await user.should_see("Discard unsaved setup edits?")
    user.find("Discard and load").click()
    await user.should_see("Loaded bench")
    assert field("setup-frame-x").value == 99

    # Another process rewrites the file under an edit, adding a section the
    # Setup panel does not edit: Merge keeps both sides.
    field("setup-frame-x").set_value(30)
    theirs = (
        store.load("bench")
        .with_parameter("speed", Parameter(5.0, "mm/s"))
        .with_camera("overhead", OVERHEAD)
    )
    (tmp_path / "bench.py").write_text(export_snapshot(theirs))
    user.find(marker="setup-save").click()
    await user.should_see(marker="setup-replace-merge")
    assert store.load("bench") == theirs
    user.find(marker="setup-replace-merge").click()
    await user.should_see("Merged your edits into bench")
    merged = store.load("bench")
    assert merged.frames["fixture"].values[0] == 30
    assert merged.parameters["speed"].value == 5.0
    assert merged.poses == theirs.poses
    assert set(merged.cameras) == {"overhead"}

    # A save from elsewhere in Commander reloads the clean panel...
    store.save("bench", merged.with_frame("fixture", Frame((50, 0, 0, 0, 0, 0))))
    await user.should_see("Loaded bench")
    assert field("setup-frame-x").value == 50

    # ...and only warns an edited one, whose Overwrite replaces the whole file
    # with what the panel holds, including the section it never edited.
    field("setup-frame-x").set_value(60)
    store.save("bench", SetupSnapshot(frames={"tray": Frame((1, 0, 0, 0, 0, 0))}))
    await user.should_see("bench was saved elsewhere")
    user.find(marker="setup-save").click()
    await user.should_see(marker="setup-replace-overwrite")
    user.find(marker="setup-replace-overwrite").click()
    await user.should_see("Saved bench")
    overwritten = store.load("bench")
    assert set(overwritten.frames) == {"fixture"}
    assert overwritten.frames["fixture"].values[0] == 60
    assert overwritten.parameters["speed"].value == 5.0
    assert set(overwritten.cameras) == {"overhead"}, (
        "saving the Setup panel dropped the camera"
    )
