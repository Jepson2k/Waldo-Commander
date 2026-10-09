"""Actual PAR6 prediction, inspected in the running Commander scene.

Runs against a live ``par6d --sim`` in its own pytest process, like the other
par6 end-to-end tests (``WALDO_PAR6_E2E=1``; see ``test_par6_backend.py``)."""

import asyncio

import numpy as np
import pytest
import waldoctl
from nicegui import core
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import camera_kept, program_previewed
from tests.helpers.scene_surface import pointer_to, scene_js, wait_for_scene_idle
from tests.helpers.visual_evidence import VisualEvidence
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_par6_backend import par6_env, requires_par6  # noqa: F401
from waldo_commander.state import ui_state
from waldo_commander.components.playback import playback
from waldo_commander.services.trajectory_comparison import comparison_samples
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode


@requires_par6
@pytest.mark.browser
@pytest.mark.usefixtures("par6_env")
def test_predicted_deviations_and_paired_inspection(screen):
    from par6 import config

    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=60)
    assert ui_state.active_robot.backend_package == "par6"
    assert waldoctl.commander.status.simulator_active
    start = np.degrees(config.homing_ready_pose_rad()).tolist()
    asyncio.run_coroutine_threadsafe(
        ui_state.control_panel.client.teleport(start), core.loop
    ).result(10)
    waypoints = []
    for changes in (
        (-60, -15, 10),
        (60, -15, 10),
        (60, 15, -10),
        (-60, 15, -10),
        (0, 0, 0),
    ):
        target = start.copy()
        target[:3] = [v + delta for v, delta in zip(start[:3], changes)]
        waypoints.append(np.round(target, 2).tolist())
    source = (
        "from par6 import RobotClient\nrbt = RobotClient()\n"
        + "\n".join(f"rbt.move_j({target}, speed=0.6)" for target in waypoints)
        + "\nrbt.delay(0.5)\n"
    )
    with (
        viewport(screen, 1920, 1080),
        camera_kept(screen),
        program_previewed(source),
    ):
        click_tab(screen, "program")
        evidence = VisualEvidence(
            screen, "review-corrections/comparison", synchronized_frames=True
        )
        appearance = ui_state.urdf_scene._appearance_mode
        try:
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                [0.14, -0.29, 0.53, 0, 0.2, 0.25],
            )
            wait(screen, 30).until(
                lambda _: screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-comparison-bar"
                ).is_displayed()
                and screen.selenium.find_element(
                    By.XPATH, "//button[normalize-space()='Peak']"
                ).is_enabled()
            )
            comparison = run_in_app(lambda: waldoctl.commander.programs.active.dry_run)
            assert comparison.predicted_current is not None
            assert comparison.commanded.stop == "completed"
            assert np.ptp(comparison.commanded.tcp[:, 0]) > 0.4
            assert comparison.commanded.digest != comparison.predicted_current.digest
            expected = comparison_samples(
                comparison.commanded, comparison.predicted_current, [False] * 6
            )
            candidates = []
            for record_index, errors in (
                (1, expected),
                (0, expected["idealSamples"]),
            ):
                for row, values in enumerate(
                    zip(
                        errors["positionMm"],
                        errors["orientationDeg"],
                        errors["jointDeg"],
                    )
                ):
                    if all(value is not None for value in values):
                        candidates.append(
                            (
                                max(
                                    value / limit
                                    for value, limit in zip(values, (1, 0.5, 0.5))
                                ),
                                record_index,
                                row,
                            )
                        )
            _, peak_record, peak = max(candidates, key=lambda candidate: candidate[0])
            selected_record = (
                comparison.predicted_current if peak_record else comparison.commanded
            )
            ideal_peak = expected["idealRows"][peak] if peak_record else peak
            wait_for_scene_idle(screen)
            evidence.capture("01-deviation-overview-1920")
            screen.selenium.find_element(
                By.XPATH, "//button[normalize-space()='Peak']"
            ).click()
            wait(screen).until(
                lambda _: scene_js(
                    screen, "return S.byName('comparison:ghost')?.visible"
                )
            )
            wait_for_scene_idle(screen)
            evidence.capture("02-paired-peak-1920")
            assert scene_js(screen, "return S.inspection().current.row") == peak
            wait(screen).until(
                lambda _: run_in_app(lambda: playback._scrub_slider.value)
                == pytest.approx(ideal_peak * comparison.commanded.row_dt_s)
            )
            assert (
                "Deviation"
                in screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-comparison-details"
                ).text
            )
            assert (
                scene_js(screen, "return S.inspection().current.record") == peak_record
            )
            wait(screen).until(
                lambda _: np.allclose(
                    waldoctl.commander.status.joints.angles.rad,
                    selected_record.joints_rad[peak],
                    atol=1e-4,
                )
            )
            card = screen.selenium.find_element(By.CSS_SELECTOR, ".wc-scene canvas")
            card.send_keys(Keys.ARROW_RIGHT)
            wait(screen).until(
                lambda _: scene_js(screen, "return S.inspection().current.row")
                == peak + 1
            )
            wait(screen).until(
                lambda _: np.allclose(
                    waldoctl.commander.status.joints.angles.rad,
                    selected_record.joints_rad[peak + 1],
                    atol=1e-4,
                )
            )
            evidence.capture("03-paired-scrub-1920")
            ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
            point = scene_js(
                screen,
                """
                const c=getElement(S.root).core;
                const points=S.inspection().records[0].blocks.flatMap(b=>S.pathPixels(0,b.command)||[]);
                points.sort((a,b)=>Math.abs(a[2]-arguments[0])-Math.abs(b[2]-arguments[0]));
                return points.find(p=>document.elementFromPoint(p[0],p[1])===S.canvas &&
                    !c.pointer.hit(p[0],p[1]) && !c.pointer.handlesAt(p[0],p[1]).length);
                """,
                ideal_peak,
            )
            assert point, "The larger ideal path needs an unobscured picking location"
            action = ActionChains(screen.selenium)
            pointer_to(screen, *point[:2], action)
            action.click().perform()
            wait(screen).until(
                lambda _: scene_js(screen, "return S.inspection().current.record") == 0
            )
            ideal_row = scene_js(screen, "return S.inspection().current.row")
            wait(screen).until(
                lambda _: np.allclose(
                    waldoctl.commander.status.joints.angles.rad,
                    comparison.commanded.joints_rad[ideal_row],
                    atol=1e-4,
                )
            )
            evidence.capture("03b-commanded-simulator-1920")
            screen.selenium.find_element(
                By.XPATH, "//button[normalize-space()='Peak']"
            ).click()
            wait(screen).until(
                lambda _: scene_js(screen, "return S.inspection().current.row") == peak
            )
            screen.selenium.find_element(
                By.CSS_SELECTOR, ".wc-comparison-details > summary"
            ).click()
            evidence.capture("03a-expanded-differences-1920")
            screen.selenium.find_element(
                By.CSS_SELECTOR, ".wc-comparison-details > summary"
            ).click()
            for _ in range(8):
                card.send_keys(Keys.ARROW_RIGHT)
            wait_for_scene_idle(screen)
            selected = scene_js(screen, "return S.inspection().current")
            # The app applies each press's sample as it handles it.
            wait(screen).until(
                lambda _: np.allclose(
                    waldoctl.commander.status.joints.angles.rad,
                    selected_record.joints_rad[selected["row"]],
                    atol=1e-4,
                )
            )
            screen.selenium.find_element(
                By.XPATH, "//button[normalize-space()='Peak']"
            ).click()
            run_in_app(
                lambda: ui_state.urdf_scene.set_appearance_mode(
                    RobotAppearanceMode.LIVE
                )
            )
            wait_for_scene_idle(screen)
            evidence.capture("04-gray-paired-1920")
            evidence.stop()
            close_panel(screen, "program-panel")
            with viewport(screen, 1366, 768):
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.30, -0.36, 0.60, 0.14, 0.2, 0.25],
                )
                wait_for_scene_idle(screen)
                evidence.capture("05-paired-peak-1366")
                screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-comparison-bar > details > summary"
                ).click()
                screen.selenium.find_element(
                    By.CSS_SELECTOR, '[aria-label="Next deviation"]'
                ).click()
                wait_for_scene_idle(screen)
                evidence.capture("06-next-deviation-1366")
                mode = Select(
                    screen.selenium.find_element(
                        By.CSS_SELECTOR,
                        '[aria-label="Prediction comparison visibility"]',
                    )
                )
                mode.select_by_value("off")
                wait(screen).until(
                    lambda _: not scene_js(
                        screen, "return !!S.byName('comparison:predicted')"
                    )
                )
                mode.select_by_value("all")
                wait(screen).until(
                    lambda _: scene_js(
                        screen, "return !!S.byName('comparison:predicted')"
                    )
                )
                evidence.capture("07-show-all-1366")
            evidence.finish()
            from fastmcp import Client
            from waldo_commander.mcp.server import get_mcp

            async def external_visibility(on):
                async with Client(get_mcp()) as client:
                    await client.call_tool(
                        "settings.set_view", {"predicted_visible": on}
                    )

            for on in (False, True):
                asyncio.run_coroutine_threadsafe(
                    external_visibility(on), core.loop
                ).result(15)
                wait(screen).until(
                    lambda _: bool(
                        scene_js(screen, "return !!S.byName('comparison:predicted')")
                    )
                    == on
                )
            click_tab(screen, "program")
            try:
                click_marked(screen, "editor-step-program")
                wait(screen, 30).until(lambda _: run_in_app(is_any_program_running))
                wait(screen, 30).until(
                    lambda _: run_in_app(
                        lambda: waldoctl.commander.programs.active.dry_run.playback.executing_step_at_end
                    )
                )
                wait(screen).until(
                    lambda _: not scene_js(
                        screen,
                        "return S.inspection().current || S.byName('comparison:ghost')?.visible",
                    )
                )
                assert not screen.selenium.find_element(
                    By.XPATH, "//button[normalize-space()='Peak']"
                ).is_enabled()
                timeline = run_in_app(lambda: playback._scrub_slider.value)
                sequence = scene_js(
                    screen,
                    """
                    const c=getElement(S.root).core,i=c.inspection,hit=i.comparison.peak;
                    i.current=hit;
                    c.emit('inspect',{...hit,revision:i.data.revision,seq:++i.seq,pinned:true});
                    return i.seq;
                """,
                )
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: ui_state.urdf_scene.inspection.sequence >= sequence
                        and not ui_state.urdf_scene.inspection.pinned
                    )
                )
                wait(screen).until(
                    lambda _: not scene_js(screen, "return S.inspection().pinned")
                )
                assert run_in_app(lambda: playback._scrub_slider.value) == timeline
            finally:
                if run_in_app(is_any_program_running):
                    click_marked(screen, "editor-stop-btn")
                    wait(screen, 20).until(
                        lambda _: not run_in_app(is_any_program_running)
                    )
            run_in_app(
                lambda: setattr(
                    ui_state.active_textarea,
                    "value",
                    source + "\n# Updated preview\n",
                )
            )
            wait(screen).until(
                lambda _: scene_js(
                    screen,
                    "return !S.byName('comparison:ghost') && !S.byName('comparison:predicted')",
                )
            )
        finally:
            evidence.stop()
            run_in_app(lambda: ui_state.urdf_scene.set_appearance_mode(appearance))
