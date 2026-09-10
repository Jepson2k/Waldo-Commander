"""Saving a setup includes pending edits across tabs without partial writes."""

import pytest
from nicegui import ui
from waldoctl.setup import Frame, Pose, SetupSnapshot

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.setup import SetupStore


@pytest.mark.integration
async def test_save_pending_fields_and_reject_partial_save(user, tmp_path, monkeypatch):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    store = SetupStore()
    store.save(
        "bench",
        SetupSnapshot(
            frames={"fixture": Frame((10, 0, 0, 0, 0, 0))},
            poses={"pick": Pose((1, 2, 3, 0, 0, 0), "fixture")},
        ),
    )
    await user.open("/")
    await wait_for_app_ready()

    def field(marker):
        return next(iter(user.find(marker=marker).elements))

    user.find(marker="tab-setup").click()
    user.find(marker="setup-load").click()
    await user.should_see("Loaded bench")
    field("setup-frame-x").set_value(20)
    user.find(kind=ui.tab, content="Poses").click()
    field("setup-pose-z").set_value(8)
    user.find(kind=ui.tab, content="Parameters").click()
    field("setup-parameter-value").set_value("12")
    user.find(marker="setup-save").click()
    await user.should_see("Saved bench")
    saved = store.load("bench")
    assert saved.resolve("pick").values[:3] == (21, 2, 8)
    assert saved.parameters["clearance"].value == 12

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
