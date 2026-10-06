"""Hover-revealed jog handles in the 3D view: a ring per joint and the TCP gizmo.

Hovering a link reveals its joint's ring and hovering the last link or the
tool reveals the gizmo; the browser does that on its own. The app tells it
which objects reveal which handle and what each ring spans, and what the
handles may do now: whether jogging is possible, the gizmo's mode, whether
target editing has them suspended. A change to that ends any drag under way.

A ring drag is the browser's proposal. The app admits it only while the ring
may move the robot, and takes each target as the change since the grab added
to the joint's value at the grab, as the app knows it.
"""

import math
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
import waldoctl
from nicegui import background_tasks

from waldo_commander.scene3d.interaction import Gesture, finite

from .angle_pipeline import urdf_to_panel

GIZMO = "gizmo"
#: Hovering the last link shows the gizmo. Set to False to give the last joint
#: a ring too and leave the gizmo to the tool.
LAST_LINK_SHOWS_GIZMO = True

#: Ring radius per joint as a fraction of the arm's reach; the last value
#: serves any further joints.
_DIAL_RADIUS_FRACTION = (0.16, 0.14, 0.12, 0.10, 0.09, 0.08)

#: (camera distance above which the band applies [m], joint step [°],
#: cartesian step [mm]), coarse to fine: how far the rings and gizmo snap.
#: The default view, 0.66 m from its target, lands in the second band.
SNAP_BANDS: tuple[tuple[float, float, float], ...] = (
    (1.2, 5.0, 10.0),
    (0.5, 1.0, 5.0),
    (0.3, 0.5, 1.0),
    (0.0, 0.1, 0.5),
)


def _z_onto(axis: np.ndarray) -> list[list[float]]:
    """Rotation taking +Z onto ``axis``, so a dial's plane is normal to its joint axis."""
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    v = np.array([-a[1], a[0], 0.0])
    c = float(a[2])
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3).tolist() if c > 0 else np.diag([1.0, -1.0, -1.0]).tolist()
    k = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    return (np.eye(3) + k + k @ k / (1.0 + c)).tolist()


class JogHandlesMixin:
    """Mixin providing the hover-revealed ring and gizmo handles for UrdfScene."""

    # Provided by UrdfScene and the other mixins.
    scene: Any
    urdf_model: Any
    joint_names: list[str]
    joint_axes: dict[str, np.ndarray]
    joint_pos_limits: dict[str, dict[str, float | None]]
    joint_groups: dict[str, Any]
    joint_frame_groups: dict[str, Any]
    _link_to_meshes: dict[str, list[Any]]
    _tool_meshes_group: Any
    _chain_reach: Callable[[], float]
    _push_interaction: Callable[[], None]
    _admitted: Callable[[], None]

    def _init_jog_handles_state(self) -> None:
        self._joint_q = np.zeros(len(self.joint_names), dtype=np.float64)
        self._hover_suspended = False
        self._handles_available = False
        self._gizmo_mode = "translate"
        self._ring_begin: Callable[[int], int | None] | None = None
        self._ring_target: Callable[[int, float], None] | None = None
        self._ring_end: Callable[[int], Awaitable[None]] | None = None
        self._ring_cancel: Callable[[int], None] | None = None

    # ---- Wiring ----

    def on_ring_drag(
        self,
        begin: Callable[[int], int | None],
        target: Callable[[int, float], None],
        end: Callable[[int], Awaitable[None]],
        cancel: Callable[[int], None],
    ) -> None:
        """Register the ring-drag sink: ``begin(joint)`` returns the drag's
        token, or None when it may not start; ``target(joint, deg)`` takes each
        snapped target in the panel's degrees; ``end(token)`` closes the drag
        and ``cancel(token)`` drops it."""
        self._ring_begin = begin
        self._ring_target = target
        self._ring_end = end
        self._ring_cancel = cancel

    def _hover_sources(self) -> dict[int, str]:
        """Which objects reveal which handle: each revolute joint's child link
        its ring, and the last link and the tool the gizmo."""
        joints = {j.name: j for j in self.urdf_model.joints}
        last = len(self.joint_names) - 1
        sources: dict[int, str] = {}
        for u, joint_name in enumerate(self.joint_names):
            joint = joints[joint_name]
            if u == last and LAST_LINK_SHOWS_GIZMO:
                target = GIZMO
            elif joint.joint_type in ("revolute", "continuous"):
                target = f"ring:{u}"
            else:
                continue
            for mesh in self._link_to_meshes.get(joint.child, []):
                sources[mesh.id] = target
        if self._tool_meshes_group is not None:
            sources[self._tool_meshes_group.id] = GIZMO
        return sources

    def _ring_specs(self) -> dict[int, dict[str, Any]]:
        """Each ring: the frame it is drawn in, the joint it turns, its radius
        and travel, and how its joint maps to the control panel's degrees."""
        names = self._panel_joint_names()
        specs: dict[int, dict[str, Any]] = {}
        sources = set(self._hover_sources().values())
        for u, joint_name in enumerate(self.joint_names):
            if f"ring:{u}" not in sources:
                continue
            frame = self.joint_frame_groups.get(joint_name)
            joint = self.joint_groups.get(joint_name)
            if frame is None or joint is None:
                continue
            limits = self.joint_pos_limits.get(joint_name, {})
            lo, hi = limits.get("min"), limits.get("max")
            if lo is None or hi is None or hi - lo >= 2 * math.pi:
                lo = hi = None
            panel, sign, offset = self._panel_mapping(u)
            fraction = _DIAL_RADIUS_FRACTION[min(u, len(_DIAL_RADIUS_FRACTION) - 1)]
            specs[u] = {
                "frame": frame.id,
                "joint": joint.id,
                "radius": fraction * self._chain_reach(),
                "lo": lo,
                "hi": hi,
                "panel": [panel, sign, offset],
                "name": names[panel] if panel < len(names) else joint_name,
                "R": _z_onto(
                    self.joint_axes.get(joint_name, np.array([0.0, 0.0, 1.0]))
                ),
            }
        return specs

    @staticmethod
    def _panel_joint_names() -> list[str]:
        from waldo_commander.state import ui_state

        robot = ui_state.active_robot
        return list(robot.joints.names) if robot is not None else []

    @staticmethod
    def _panel_mapping(u: int) -> tuple[int, float, float]:
        """(panel index, sign, offset °) of URDF joint *u*, from two points
        of its exact mapping."""
        panel, at_zero = urdf_to_panel(u, 0.0)
        _, at_one = urdf_to_panel(u, math.radians(1.0))
        sign = at_one - at_zero
        return panel, round(sign), -at_zero * round(sign)

    # ---- Rules ----

    def _handle_rules(self) -> dict[str, Any]:
        gizmo = (
            self._gizmo_mode
            if waldoctl.commander.settings.view.gizmo_visible
            else "hidden"
        )
        return {
            "available": self._handles_available,
            "gizmo": gizmo,
            "suspended": self._hover_suspended,
        }

    @staticmethod
    def _jogging(rules: dict[str, Any] | None) -> bool:
        """Whether *rules* let the handles jog the robot."""
        return rules is not None and rules["available"] and not rules["suspended"]

    def _rules_changed(self) -> None:
        """Send the handles' rules; a drag that began under rules that no
        longer allow it ends. Jogging stopping or starting again ends every
        drag; the gizmo changing ends only its own. A target edit or keep-out
        move is not a jog: what jogging may do while it suspends the handles
        leaves it alone."""
        if self.scene is None:
            return
        rules = self._handle_rules()
        old = self.scene.interaction.get("rules")
        if old == rules:
            return
        if self._jogging(old) != self._jogging(rules):
            self.scene.gestures.bump()
        elif old is not None and old["gizmo"] != rules["gizmo"]:
            self.scene.gestures.abort("tcp")
        self.scene.set_interaction(rules=rules)

    def set_handles_available(self, available: bool) -> None:
        """Whether jogging is possible at all; a shown handle goes when it is not."""
        self._handles_available = available
        self._rules_changed()

    def suspend_hover(self) -> None:
        """Hide any handle and reveal none until :meth:`resume_hover` (editing mode)."""
        self._hover_suspended = True
        self._rules_changed()

    def resume_hover(self) -> None:
        self._hover_suspended = False
        self._rules_changed()

    def refresh_handles(self) -> None:
        """Re-check the handles against the view settings (the gizmo set to Hidden)."""
        self._rules_changed()

    def _ring_allowed(self) -> bool:
        return self._handles_available and not self._hover_suspended

    # ---- Ring drags ----

    def _ring_admit(self, gesture: Gesture, args: dict[str, Any]) -> bool:
        panel = args.get("joint")
        if not self._ring_allowed() or self._ring_begin is None:
            return False
        for u, spec in self._ring_specs().items():
            if spec["panel"][0] == panel:
                break
        else:
            return False
        token = self._ring_begin(panel)
        if token is None:
            return False
        anchor = urdf_to_panel(u, self._joint_q[u])[1]
        gesture.data.update(panel=panel, anchor=anchor, token=token)
        self._admitted()
        return True

    def _ring_move(self, gesture: Gesture, args: dict[str, Any]) -> None:
        values = finite(args, "delta")
        if values is None or self._ring_target is None:
            return
        self._ring_target(gesture.data["panel"], gesture.data["anchor"] + values[0])

    def _ring_finish(
        self, gesture: Gesture, args: dict[str, Any] | None, aborted: bool
    ) -> None:
        token = gesture.data["token"]
        if aborted or args is None:
            if self._ring_cancel is not None:
                self._ring_cancel(token)
            return
        self._ring_move(gesture, args)
        if self._ring_end is not None:
            background_tasks.create(self._ring_end(token), name="ring drag end")


__all__ = ["GIZMO", "LAST_LINK_SHOWS_GIZMO", "SNAP_BANDS", "JogHandlesMixin"]
