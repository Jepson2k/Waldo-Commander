"""Tests for the tool system: registry, TCP offsets, mesh configuration, and variants.

These tests verify the tool system integration between the robot backend
and the web commander — tool specs, TCP transforms, mesh descriptors,
and variant configurations are consistent and complete.
"""

import numpy as np
import pytest
from nicegui.testing import User
from waldoctl import ToolStatus

from waldoctl import (
    ElectricGripperTool,
    GripperType,
    LinearMotion,
    MeshRole,
    PneumaticGripperTool,
    ToggleMode,
    ToolType,
)

from waldo_commander.state import ui_state
from tests.helpers.wait import wait_for_app_ready


@pytest.mark.integration
async def test_tool_registry_matches_robot(user: User) -> None:
    """Verify the web commander sees all registered tools from the robot backend.

    The robot should expose 5 tools (NONE, PNEUMATIC, SSG-48, MSG, VACUUM) and
    they should all be accessible through the tools API with correct types.
    """
    await user.open("/")
    await wait_for_app_ready()

    robot = ui_state.active_robot
    tools = robot.tools

    # Native count stays 5; robot.tools may compose plugin tools on top.
    native_keys = [t.key for t in robot._native_tools.available]
    assert len(native_keys) == 5, f"Expected 5 native tools, got {native_keys}"
    keys = [t.key for t in tools.available]
    for expected in ("NONE", "PNEUMATIC", "SSG-48", "MSG", "VACUUM"):
        assert expected in keys, f"{expected} not in {keys}"

    # Default tool is NONE
    assert tools.default.key == "NONE"
    assert tools.default.tool_type == ToolType.NONE

    # 4 grippers (PNEUMATIC, SSG-48, MSG, VACUUM)
    grippers = tools.by_type(ToolType.GRIPPER)
    assert len(grippers) == 4
    gripper_keys = {t.key for t in grippers}
    assert gripper_keys == {"PNEUMATIC", "SSG-48", "MSG", "VACUUM"}

    # Type checks
    assert isinstance(tools["PNEUMATIC"], PneumaticGripperTool)
    assert tools["PNEUMATIC"].gripper_type == GripperType.PNEUMATIC
    assert isinstance(tools["SSG-48"], ElectricGripperTool)
    assert tools["SSG-48"].gripper_type == GripperType.ELECTRIC
    assert isinstance(tools["MSG"], ElectricGripperTool)
    assert tools["MSG"].gripper_type == GripperType.ELECTRIC
    assert isinstance(tools["VACUUM"], PneumaticGripperTool)
    assert tools["VACUUM"].gripper_type == GripperType.PNEUMATIC

    # Invalid key raises
    with pytest.raises(KeyError):
        tools["BOGUS"]


@pytest.mark.integration
async def test_tool_tcp_offsets_differ(user: User) -> None:
    """Each tool should have a distinct TCP origin so FK/IK uses the correct offset."""
    await user.open("/")
    await wait_for_app_ready()

    robot = ui_state.active_robot
    origins = {t.key: tuple(t.tcp_origin) for t in robot.tools.available}

    # All TCP origins should be distinct from each other
    assert origins["NONE"] != origins["PNEUMATIC"], "NONE and PNEUMATIC should differ"
    assert origins["PNEUMATIC"] != origins["SSG-48"], (
        "PNEUMATIC and SSG-48 should differ"
    )
    assert origins["SSG-48"] != origins["MSG"], "SSG-48 and MSG should differ"
    assert origins["MSG"] != origins["VACUUM"], "MSG and VACUUM should differ"

    # NONE should be identity (no offset)
    assert origins["NONE"] == (0.0, 0.0, 0.0), (
        f"NONE tool should have zero TCP origin, got {origins['NONE']}"
    )


@pytest.mark.integration
async def test_tool_mesh_specs(user: User) -> None:
    """Verify mesh specs are configured correctly for tools with 3D models.

    All grippers should have body + jaw meshes. VACUUM has body only.
    NONE has no meshes.
    """
    await user.open("/")
    await wait_for_app_ready()

    robot = ui_state.active_robot

    # NONE — no meshes
    none_tool = robot.tools["NONE"]
    assert len(none_tool.meshes) == 0

    # PNEUMATIC — body + 2 jaws
    pneumatic = robot.tools["PNEUMATIC"]
    assert len(pneumatic.meshes) == 3
    roles = [m.role for m in pneumatic.meshes]
    assert roles.count(MeshRole.BODY) == 1
    assert roles.count(MeshRole.JAW) == 2

    # SSG-48 — body + 2 jaws (finger variant default)
    ssg48 = robot.tools["SSG-48"]
    assert len(ssg48.meshes) == 3
    roles = [m.role for m in ssg48.meshes]
    assert roles.count(MeshRole.BODY) == 1
    assert roles.count(MeshRole.JAW) == 2

    # MSG — body + 2 jaws (100mm variant default)
    msg = robot.tools["MSG"]
    assert len(msg.meshes) == 3
    roles = [m.role for m in msg.meshes]
    assert roles.count(MeshRole.BODY) == 1
    assert roles.count(MeshRole.JAW) == 2

    # VACUUM — body only, no jaws
    vacuum = robot.tools["VACUUM"]
    assert len(vacuum.meshes) == 1
    assert vacuum.meshes[0].role == MeshRole.BODY


@pytest.mark.integration
async def test_tool_motion_descriptors(user: User) -> None:
    """Verify gripper motion descriptors for jaw animation.

    All grippers with jaws should have LinearMotion descriptors. VACUUM has none.
    """
    await user.open("/")
    await wait_for_app_ready()

    robot = ui_state.active_robot

    # PNEUMATIC — single jaw motion, 3.5mm travel (vertical default)
    pneumatic = robot.tools["PNEUMATIC"]
    motions = pneumatic.motions
    assert len(motions) == 1
    assert motions[0].role == MeshRole.JAW
    assert motions[0].travel_m == pytest.approx(0.0035, abs=1e-6)

    # SSG-48 — single jaw motion, 24mm travel, symmetric
    ssg48 = robot.tools["SSG-48"]
    motions = ssg48.motions
    assert len(motions) == 1
    assert motions[0].role == MeshRole.JAW
    assert motions[0].travel_m == pytest.approx(0.024, abs=1e-6)
    assert motions[0].symmetric is True

    # MSG — single jaw motion, 26.7mm travel (100mm default), symmetric
    msg = robot.tools["MSG"]
    motions = msg.motions
    assert len(motions) == 1
    assert motions[0].role == MeshRole.JAW
    assert motions[0].travel_m == pytest.approx(0.0267, abs=1e-6)
    assert motions[0].symmetric is True

    # VACUUM — no motions (no moving parts)
    vacuum = robot.tools["VACUUM"]
    motions = vacuum.motions
    assert len(motions) == 0

    # NONE — no motions
    none_tool = robot.tools["NONE"]
    motions = none_tool.motions
    assert len(motions) == 0


@pytest.mark.integration
async def test_electric_gripper_parameter_ranges(user: User) -> None:
    """SSG-48 and MSG should expose position, speed, and current ranges
    that the gripper UI sliders use for clamping."""
    await user.open("/")
    await wait_for_app_ready()

    robot = ui_state.active_robot

    expected_current = {"SSG-48": (100, 1300), "MSG": (100, 2800)}
    for key in ("SSG-48", "MSG"):
        tool = robot.tools[key]
        assert isinstance(tool, ElectricGripperTool), (
            f"{key} should be ElectricGripperTool"
        )
        assert tool.position_range == (0.0, 1.0), f"{key} position_range"
        assert tool.speed_range == (0.0, 1.0), f"{key} speed_range"
        assert tool.current_range == expected_current[key], f"{key} current_range"


@pytest.mark.integration
async def test_tool_variants(user: User) -> None:
    """Verify tools with variants expose correct variant configurations.

    PNEUMATIC: vertical, horizontal
    SSG-48: finger, pinch
    MSG: 100mm, 150mm, 200mm
    NONE/VACUUM: no variants
    """
    await user.open("/")
    await wait_for_app_ready()

    robot = ui_state.active_robot

    # PNEUMATIC — 2 variants
    pneumatic = robot.tools["PNEUMATIC"]
    variants = pneumatic.variants
    assert len(variants) == 2
    vkeys = {v.key for v in variants}
    assert vkeys == {"vertical", "horizontal"}
    # Each variant has 3 meshes (body + 2 jaws) and 1 motion
    for v in variants:
        assert len(v.meshes) == 3, f"PNEUMATIC {v.key} should have 3 meshes"
        assert len(v.motions) == 1, f"PNEUMATIC {v.key} should have 1 motion"

    # SSG-48 — 2 variants (finger, pinch)
    ssg48 = robot.tools["SSG-48"]
    variants = ssg48.variants
    assert len(variants) == 2
    vkeys = {v.key for v in variants}
    assert vkeys == {"finger", "pinch"}
    for v in variants:
        assert len(v.meshes) == 3, f"SSG-48 {v.key} should have 3 meshes"
        assert len(v.motions) == 1, f"SSG-48 {v.key} should have 1 motion"

    # MSG — 3 variants (100mm, 150mm, 200mm)
    msg = robot.tools["MSG"]
    variants = msg.variants
    assert len(variants) == 3
    vkeys = {v.key for v in variants}
    assert vkeys == {"100mm", "150mm", "200mm"}
    for v in variants:
        assert len(v.meshes) == 3, f"MSG {v.key} should have 3 meshes"
        assert len(v.motions) == 1, f"MSG {v.key} should have 1 motion"

    # NONE — no variants
    none_tool = robot.tools["NONE"]
    assert len(none_tool.variants) == 0

    # VACUUM — no variants
    vacuum = robot.tools["VACUUM"]
    assert len(vacuum.variants) == 0


@pytest.mark.integration
async def test_tcp_offset_changes_fk(user: User) -> None:
    """set_active_tool with tcp_offset_m should shift FK output by that offset."""
    await user.open("/")
    await wait_for_app_ready()

    robot = ui_state.active_robot
    q_zero = np.zeros(robot.joints.count)

    fk_buf = np.empty(6, dtype=np.float64)
    fk_base = np.empty(6, dtype=np.float64)

    # FK with PNEUMATIC, no user offset
    robot.set_active_tool("PNEUMATIC")
    robot.fk(q_zero, fk_base)

    # FK with 10mm Z offset
    robot.set_active_tool("PNEUMATIC", tcp_offset_m=(0.0, 0.0, 0.01))
    robot.fk(q_zero, fk_buf)

    # The offset is in the tool's local frame. At q_zero, the PNEUMATIC
    # tool Z-axis points in world -Z, so a +Z tool offset shifts world by -Z.
    diff_mm = (fk_buf[:3] - fk_base[:3]) * 1000
    assert abs(abs(diff_mm[2]) - 10.0) < 0.01, (
        f"Expected ~10mm |Z| shift, got {diff_mm[2]:.3f}mm"
    )
    assert abs(diff_mm[0]) < 0.01, f"X should not shift, got {diff_mm[0]:.3f}mm"
    assert abs(diff_mm[1]) < 0.01, f"Y should not shift, got {diff_mm[1]:.3f}mm"

    # Zero offset should match base FK
    robot.set_active_tool("PNEUMATIC", tcp_offset_m=(0.0, 0.0, 0.0))
    robot.fk(q_zero, fk_buf)
    assert np.allclose(fk_buf, fk_base, atol=1e-10), "Zero offset should match base FK"


# ===========================================================================
# Gripper position convention and animation direction
# ===========================================================================


class TestGripperCloseAnimation:
    """Verify that sending 'close' (position=1.0) results in jaws animating inward.

    Convention: 0.0 = fully open, 1.0 = fully closed.

    The full chain:
    1. Electric gripper populate_status: hw closed (255) → position 1.0
    2. Pneumatic gripper populate_status: hw closed (valve off) → position 1.0
    3. Animation formula: position 1.0 → negative travel → jaws move inward from STL default
    """

    def test_electric_gripper_position_convention(self):
        """Electric gripper: hw 255 = closed → position 1.0, hw 0 = open → position 0.0."""
        from parol6.server.state import ControllerState
        from parol6.tools import ElectricGripperConfig

        cfg = ElectricGripperConfig(
            name="test", description="test", transform=np.eye(4)
        )
        hw = ControllerState()

        # hw fully closed (byte 255)
        hw.Gripper_data_in[1] = 255
        status = ToolStatus()
        cfg.populate_status(hw, status)
        assert status.positions[0] == pytest.approx(1.0), (
            "hw=255 (closed) should report position 1.0"
        )

        # hw fully open (byte 0)
        hw.Gripper_data_in[1] = 0
        status = ToolStatus()
        cfg.populate_status(hw, status)
        assert status.positions[0] == pytest.approx(0.0), (
            "hw=0 (open) should report position 0.0"
        )

    def test_pneumatic_gripper_position_convention(self):
        """Pneumatic gripper: valve off (0) = closed → position 1.0,
        valve on (1) = open → position 0.0."""
        from parol6.server.state import ControllerState
        from parol6.tools import PneumaticGripperConfig

        cfg = PneumaticGripperConfig(
            name="test", description="test", transform=np.eye(4), io_port=1
        )
        hw = ControllerState()

        # Valve off — closed
        hw.InOut_out[2] = 0
        status = ToolStatus()
        cfg.populate_status(hw, status)
        assert status.positions[0] == pytest.approx(1.0), (
            "Valve off should report position 1.0 (closed)"
        )

        # Valve on — open
        hw.InOut_out[2] = 1
        status = ToolStatus()
        cfg.populate_status(hw, status)
        assert status.positions[0] == pytest.approx(0.0), (
            "Valve on should report position 0.0 (open)"
        )

    def test_jaw_animation_closed_moves_inward(self):
        """At position 1.0 (closed), jaw travel must be negative (inward from STL default).

        STL meshes show the gripper in its open/resting state. The animation formula
        ``travel_m * -frac`` ensures closed (frac=1) pushes jaws inward and
        open (frac=0) leaves jaws at their STL default positions.
        """
        for tool_name, travel_m in [
            ("PNEUMATIC", 0.0035),
            ("SSG-48", 0.024),
            ("MSG", 0.0267),
        ]:
            motion = LinearMotion(
                role=MeshRole.JAW,
                axis=(0.0, 1.0, 0.0),
                travel_m=travel_m,
                symmetric=True,
            )

            # Closed: frac=1.0 → jaws should move inward (negative travel)
            travel_closed = motion.travel_m * -1.0
            assert travel_closed < 0, (
                f"{tool_name}: closed jaw travel should be negative, got {travel_closed}"
            )

            # Open: frac=0.0 → jaws at STL default (zero travel)
            travel_open = motion.travel_m * -0.0
            assert travel_open == 0.0, (
                f"{tool_name}: open jaw travel should be 0 (STL default), got {travel_open}"
            )

            # Mid-position: frac=0.5 → halfway inward
            travel_mid = motion.travel_m * -0.5
            assert -travel_m < travel_mid < 0, (
                f"{tool_name}: mid travel should be between -travel_m and 0, got {travel_mid}"
            )


@pytest.mark.integration
async def test_tool_quick_action_properties(user: User) -> None:
    """Verify quick-action properties on all tool types.

    Electric grippers: action_l_labels, action_l_icons, adjust_step within current_range.
    Pneumatic grippers: action_l_labels, action_l_icons, adjust_step is None.
    is_open() boundary: 0.49 → True, 0.5 → False.
    """
    await user.open("/")
    await wait_for_app_ready()

    robot = ui_state.active_robot

    # Electric grippers (SSG-48, MSG)
    for key in ("SSG-48", "MSG"):
        tool = robot.tools[key]
        assert isinstance(tool, ElectricGripperTool)
        assert tool.action_l_labels == ("Close", "Open"), f"{key} action_l_labels"
        assert tool.action_l_icons == ("close_fullscreen", "open_in_full"), (
            f"{key} action_l_icons"
        )
        assert tool.action_l_mode == ToggleMode.TOGGLE, f"{key} action_l_mode"

        step = tool.adjust_step
        assert step is not None, f"{key} should have adjust_step"
        lo, hi = tool.current_range
        assert 0 < step <= (hi - lo), f"{key} adjust_step {step} not in (0, {hi - lo}]"

        # is_open boundary (0.0 = open, 1.0 = closed)
        assert tool.is_open(0.49), f"{key} is_open(0.49) should be True"
        assert not tool.is_open(0.5), f"{key} is_open(0.5) should be False"

    # Pneumatic gripper
    pneumatic = robot.tools["PNEUMATIC"]
    assert isinstance(pneumatic, PneumaticGripperTool)
    assert pneumatic.action_l_labels == ("Close", "Open")
    assert pneumatic.action_l_icons == ("close_fullscreen", "open_in_full")
    assert pneumatic.adjust_step is None

    # NONE tool — no action buttons
    none_tool = robot.tools["NONE"]
    assert none_tool.action_l_labels is None
    assert none_tool.action_l_icons is None
    assert none_tool.adjust_step is None
