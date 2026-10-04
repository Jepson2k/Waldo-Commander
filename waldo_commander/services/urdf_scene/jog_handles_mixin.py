"""Hover-revealed jog handles in the 3D view: a ring per joint and the TCP gizmo.

Hovering a link reveals its joint's ring; hovering the last link or the tool
reveals the gizmo. One handle shows at a time: it goes, or gives way to the
next link's, 200 ms after the pointer leaves it. A handle that goes is deleted
rather than hidden, because the scene's raycaster hits invisible objects.

A ring drag accumulates the pointer's angle around the joint axis, snaps the
change to the scene's step, clamps it to the joint's travel and hands the
result to the control panel, which streams it to the robot.
"""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import waldoctl
from nicegui import ui
from nicegui.events import GenericEventArguments, ScenePointerEventArguments

from waldo_commander.common.theme import hex_of
from waldo_commander.state import ui_state

from .angle_pipeline import urdf_to_panel
from .scene_batch import batch_scene
from .snap import SceneSnap

GIZMO = "gizmo"
HOVER_GRACE_S = 0.2
#: Hovering the last link shows the gizmo. Set to False to give the last joint
#: a ring too and leave the gizmo to the tool.
LAST_LINK_SHOWS_GIZMO = True

#: Ring radius per joint as a fraction of the arm's reach; the last value
#: serves any further joints.
_DIAL_RADIUS_FRACTION = (0.16, 0.14, 0.12, 0.10, 0.09, 0.08)
_DIAL_TUBE_M = 0.0015
_DIAL_HIT_TUBE_M = 0.012
_KNOB_RADIUS_M = 0.006
_LABEL_GAP_M = 0.03
_TICKS_EACH_SIDE = 8
#: A pointer this close to the axis, as a fraction of the radius, has no
#: meaningful angle and is ignored.
_DEAD_ZONE = 0.15
_FOLLOW_EPS_RAD = math.radians(0.05)

HANDLE_LABEL_STYLE = (
    "background: var(--wc-glass); color: var(--wc-text);"
    " border: 1px solid var(--wc-glass-border); border-radius: var(--wc-radius-sm);"
    " padding: 2px 6px; font: 500 12px/16px var(--wc-font-mono);"
    " white-space: pre; pointer-events: none;"
)


def signed(value: float, digits: int = 1) -> str:
    """``value`` with an explicit sign and a typographic minus."""
    return f"{value:+.{digits}f}".replace("-", "\u2212")


def unsigned(value: float, digits: int = 1) -> str:
    """``value`` with a typographic minus when negative."""
    return f"{value:.{digits}f}".replace("-", "\u2212")


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


@dataclass
class _Dial:
    urdf_index: int
    panel_index: int
    name: str
    radius: float
    lo: float | None
    hi: float | None
    group: Any
    knob: Any
    label: Any
    q: float
    ticks: Any = None
    text: str = ""


@dataclass
class _DialDrag:
    q0: float
    theta: float
    acc: float = 0.0


class JogHandlesMixin:
    """Mixin providing the hover-revealed ring and gizmo handles for UrdfScene."""

    # Provided by UrdfScene and the other mixins.
    scene: Any
    urdf_model: Any
    joint_names: list[str]
    joint_axes: dict[str, np.ndarray]
    joint_pos_limits: dict[str, dict[str, float | None]]
    joint_frame_groups: dict[str, Any]
    _link_to_meshes: dict[str, list[Any]]
    _tool_meshes_group: Any
    _tcp_ball_dragging: bool
    set_gizmo_visible: Callable[[bool], None]
    _apply_gizmo_snap: Callable[[], None]
    _chain_reach: Callable[[], float]

    def _init_jog_handles_state(self) -> None:
        self.snap = SceneSnap()
        self.snap.add_listener(self._on_snap_changed)
        self._joint_q = np.zeros(len(self.joint_names), dtype=np.float64)
        self._ring_of_target: dict[str, int] = {}
        self._hover_target: str | None = None
        self._shown_handle: str | None = None
        self._hover_grace: ui.timer | None = None
        self._hover_suspended = False
        self._handles_available = False
        self._hover_pinned = False
        # Whether the pointer is on one of the gizmo's handles, which reach
        # past the arm.
        self._gizmo_hovered = False
        self._dial: _Dial | None = None
        self._dial_drag: _DialDrag | None = None
        self._dial_axes: dict[str, list[list[float]]] = {}
        self._ring_begin: Callable[[int], bool] | None = None
        self._ring_target: Callable[[int, float], None] | None = None
        self._ring_end: Callable[[], Awaitable[None]] | None = None
        self._ring_cancel: Callable[[], None] | None = None

    # ---- Wiring ----

    def on_ring_drag(
        self,
        begin: Callable[[int], bool],
        target: Callable[[int, float], None],
        end: Callable[[], Awaitable[None]],
        cancel: Callable[[], None],
    ) -> None:
        """Register the ring-drag sink: ``begin(joint)`` says whether the drag
        may start, ``target(joint, deg)`` takes each snapped target in the
        panel's degrees, and ``end()`` closes the drag."""
        self._ring_begin = begin
        self._ring_target = target
        self._ring_end = end
        self._ring_cancel = cancel

    def _register_hover_sources(self) -> None:
        """Make each link reveal its handle, and the tool reveal the gizmo."""
        joints = {j.name: j for j in self.urdf_model.joints}
        last = len(self.joint_names) - 1
        for u, joint_name in enumerate(self.joint_names):
            joint = joints[joint_name]
            if u == last and LAST_LINK_SHOWS_GIZMO:
                target = GIZMO
            elif joint.joint_type in ("revolute", "continuous"):
                target = f"ring:{u}"
                self._ring_of_target[target] = u
            else:
                continue
            for mesh in self._link_to_meshes.get(joint.child, []):
                self._watch_hover(mesh, target)
        if self._tool_meshes_group is not None:
            self._watch_hover(self._tool_meshes_group, GIZMO)
        self.scene.on_pointer_missed(self._on_pointer_missed)
        ui.on("wc_camera_distance", self._on_camera_distance)
        ui.on("wc_gizmo_hover", self._on_gizmo_hover)

    def _watch_hover(self, obj: Any, target: str) -> None:
        obj.on_pointer_over(lambda e: self._hover_enter(target, e.pointer_type))
        obj.on_pointer_out(lambda e: self._hover_leave(target))
        obj.on_click(lambda e: self._on_source_click(target, e))

    def _on_camera_distance(self, e: GenericEventArguments) -> None:
        self.snap.set_distance(float(e.args["distance"]))

    # ---- Availability ----

    def set_handles_available(self, available: bool) -> None:
        """Whether jogging is possible at all; a shown handle goes when it is not."""
        self._handles_available = available
        self.refresh_handles()

    def suspend_hover(self) -> None:
        """Hide any handle and reveal none until :meth:`resume_hover` (editing mode)."""
        self._hover_suspended = True
        self._hover_target = None
        self._hover_pinned = False
        self._cancel_grace()
        self._hide_handle()

    def resume_hover(self) -> None:
        self._hover_suspended = False

    def refresh_handles(self) -> None:
        """Re-check the shown handle against what is allowed now (e.g. the gizmo
        set to Hidden), and reveal the one under the pointer if it now is."""
        shown = self._shown_handle
        if shown is not None and not self._handle_allowed(shown):
            self._hover_pinned = False
            self._cancel_grace()
            self._hide_handle()
        target = self._hover_target
        if (
            self._shown_handle is None
            and target is not None
            and self._handle_allowed(target)
        ):
            self._show_handle(target)

    def _handle_allowed(self, target: str) -> bool:
        if self._hover_suspended or not self._handles_available:
            return False
        if target == GIZMO:
            return bool(waldoctl.commander.settings.view.gizmo_visible)
        return target in self._ring_of_target

    def _handle_dragging(self) -> bool:
        return self._dial_drag is not None or self._tcp_ball_dragging

    # ---- Hover state machine ----

    def _hover_enter(self, target: str, pointer_type: str) -> None:
        if pointer_type == "mouse":
            self._hover_pinned = False
        self._hover_target = target
        if self._shown_handle == target:
            self._cancel_grace()
        elif self._shown_handle is None:
            if self._handle_allowed(target):
                self._show_handle(target)
        else:
            self._arm_grace()

    def _hover_leave(self, target: str) -> None:
        if target == GIZMO and self._gizmo_hovered:
            return
        if self._hover_target == target:
            self._hover_target = None
        if self._shown_handle is not None:
            self._arm_grace()

    def _on_source_click(self, target: str, e: ScenePointerEventArguments) -> None:
        """A tap pins the handle, since touch has no hover to keep it."""
        if e.pointer_type != "touch" or not self._handle_allowed(target):
            return
        self._hover_target = target
        self._hover_pinned = True
        self._cancel_grace()
        if self._shown_handle != target and not self._handle_dragging():
            self._hide_handle()
            self._show_handle(target)

    def _on_pointer_missed(self, e: Any) -> None:
        if e.type == "click" and self._hover_pinned and not self._handle_dragging():
            self._hover_pinned = False
            self._hover_target = None
            self._hide_handle()

    def _arm_grace(self) -> None:
        self._cancel_grace()
        with self.scene:
            self._hover_grace = ui.timer(HOVER_GRACE_S, self._on_grace, once=True)

    def _cancel_grace(self) -> None:
        if self._hover_grace is not None:
            self._hover_grace.cancel()
            self._hover_grace = None

    def _on_gizmo_hover(self, e: GenericEventArguments) -> None:
        hovered = e.args["name"] == "tcp:ball"
        if hovered == self._gizmo_hovered:
            return
        self._gizmo_hovered = hovered
        if hovered:
            self._hover_enter(GIZMO, "mouse")
        else:
            self._hover_leave(GIZMO)

    def _on_grace(self) -> None:
        self._hover_grace = None
        # A drag keeps its handle; the drag's end settles the hover again.
        if self._hover_pinned or self._handle_dragging():
            return
        # The gizmo under the pointer wins over the arm behind it.
        if self._gizmo_hovered and self._shown_handle == GIZMO:
            return
        target = self._hover_target
        if target == self._shown_handle:
            return
        self._hide_handle()
        if target is not None and self._handle_allowed(target):
            self._show_handle(target)

    def _settle_hover(self) -> None:
        """After a drag: go to whatever the pointer is over now, after the grace."""
        if self._hover_target != self._shown_handle and not self._hover_pinned:
            self._arm_grace()

    def _show_handle(self, target: str) -> None:
        self._shown_handle = target
        if target == GIZMO:
            self.set_gizmo_visible(True)
        else:
            self._show_dial(self._ring_of_target[target])

    def _hide_handle(self) -> None:
        shown = self._shown_handle
        if shown is None:
            return
        self._shown_handle = None
        if shown == GIZMO:
            self.set_gizmo_visible(False)
        else:
            self._hide_dial()

    # ---- Snap ----

    def _on_snap_changed(self) -> None:
        dial = self._dial
        if dial is not None:
            with batch_scene(self.scene):
                self._draw_dial_ticks(dial)
                self._apply_dial_drag()
                self._update_dial_label(dial)
        self._apply_gizmo_snap()

    # ---- Ring ----

    def _dial_radius(self, u: int) -> float:
        fraction = _DIAL_RADIUS_FRACTION[min(u, len(_DIAL_RADIUS_FRACTION) - 1)]
        return fraction * self._chain_reach()

    def _dial_orientation(self, joint_name: str) -> list[list[float]]:
        R = self._dial_axes.get(joint_name)
        if R is None:
            axis = self.joint_axes.get(joint_name, np.array([0.0, 0.0, 1.0]))
            R = self._dial_axes[joint_name] = _z_onto(axis)
        return R

    def _show_dial(self, u: int) -> None:
        joint_name = self.joint_names[u]
        frame = self.joint_frame_groups.get(joint_name)
        if frame is None or self.scene is None:
            return
        limits = self.joint_pos_limits.get(joint_name, {})
        lo, hi = limits.get("min"), limits.get("max")
        if lo is None or hi is None or hi - lo >= 2 * math.pi:
            lo = hi = None
        start, arc = (0.0, 2 * math.pi) if lo is None or hi is None else (lo, hi - lo)
        radius = self._dial_radius(u)
        q = float(self._joint_q[u])
        panel_index, _ = urdf_to_panel(u, q)
        names = ui_state.active_robot.joints.names
        name = names[panel_index] if panel_index < len(names) else joint_name
        target = f"ring:{u}"
        action = hex_of("action")
        with self.scene, batch_scene(self.scene), frame:
            group = ui.scene.group().with_name(f"jog:dial:{u}")
            group.rotate_R(self._dial_orientation(joint_name))
            with group:
                ui.scene.torus(radius, _DIAL_TUBE_M, 8, 96, arc).rotate(
                    0.0, 0.0, start
                ).material(action, 0.55)
                # Not drawn, but the raycaster hits it: a grip wider than the track.
                ui.scene.torus(radius, _DIAL_HIT_TUBE_M, 6, 64, arc).rotate(
                    0.0, 0.0, start
                ).visible(False)
                knob = ui.scene.group().with_name(f"jog:dial:{u}:knob")
                knob.rotate(0.0, 0.0, q)
                with knob:
                    ui.scene.sphere(_KNOB_RADIUS_M, 16, 12).move(
                        radius, 0.0, 0.0
                    ).material(action)
                    label = (
                        ui.scene.text("", HANDLE_LABEL_STYLE)
                        .with_name(f"jog:dial:{u}:label")
                        .move(radius + _LABEL_GAP_M, 0.0, 0.0)
                    )
            group.on_pointer_over(lambda e: self._hover_enter(target, e.pointer_type))
            group.on_pointer_out(lambda e: self._hover_leave(target))
            group.on_pointer_down(self._on_dial_down)
            group.on_pointer_move(self._on_dial_move)
            group.on_pointer_up(self._on_dial_up)
            group.capture_pointer()
            dial = _Dial(
                urdf_index=u,
                panel_index=panel_index,
                name=name,
                radius=radius,
                lo=lo,
                hi=hi,
                group=group,
                knob=knob,
                label=label,
                q=q,
            )
            self._dial = dial
            self._draw_dial_ticks(dial)
            self._update_dial_label(dial)

    def _hide_dial(self) -> None:
        dial = self._dial
        if dial is None:
            return
        self._dial = None
        if self._dial_drag is not None:
            self._dial_drag = None
            if self._ring_cancel is not None:
                self._ring_cancel()
        dial.group.delete()

    def _draw_dial_ticks(self, dial: _Dial) -> None:
        """Dots at whole steps either side of where the knob is, or where the drag began.

        Redrawn only when the step changes; following the knob is a rotation.
        """
        step = math.radians(self.snap.joint_deg)
        r = dial.radius
        points = [
            [r * math.cos(k * step), r * math.sin(k * step), 0.0]
            for k in range(-_TICKS_EACH_SIDE, _TICKS_EACH_SIDE + 1)
        ]
        anchor = dial.q if self._dial_drag is None else self._dial_drag.q0
        if dial.ticks is not None:
            dial.ticks.delete()
        with self.scene, dial.group:
            dial.ticks = (
                ui.scene.point_cloud(
                    points, point_size=max(0.0008, min(0.003, 0.5 * r * step))
                )
                .with_name(f"jog:dial:{dial.urdf_index}:ticks")
                .rotate(0.0, 0.0, anchor)
                .material(hex_of("text-muted"))
            )

    def _update_dial_label(self, dial: _Dial) -> None:
        _, deg = urdf_to_panel(dial.urdf_index, dial.q)
        text = f"{dial.name}  {unsigned(deg)}°"
        drag = self._dial_drag
        if drag is not None:
            _, deg0 = urdf_to_panel(dial.urdf_index, drag.q0)
            text += f"  Δ{signed(deg - deg0)}°"
        text += f"  step {self.snap.joint_deg:g}°"
        if text != dial.text:
            dial.text = text
            dial.label.set_text(text)

    def _place_knob(self, dial: _Dial, q: float) -> None:
        dial.q = q
        dial.knob.rotate(0.0, 0.0, q)
        if self._dial_drag is None and dial.ticks is not None:
            dial.ticks.rotate(0.0, 0.0, q)
        self._update_dial_label(dial)

    def _follow_dial(self) -> None:
        """Keep the shown ring's knob on the live joint value while not dragging."""
        dial = self._dial
        if dial is None or self._dial_drag is not None:
            return
        q = float(self._joint_q[dial.urdf_index])
        if abs(q - dial.q) >= _FOLLOW_EPS_RAD:
            self._place_knob(dial, q)

    def _on_dial_down(self, e: ScenePointerEventArguments) -> None:
        dial = self._dial
        # None: the pointer ray missed the ring's plane.
        x, y = e.x, e.y
        if (
            dial is None
            or self._dial_drag is not None
            or e.button != 0
            or x is None
            or y is None
            or not (math.isfinite(x) and math.isfinite(y))
            or self._ring_begin is None
        ):
            return
        if not self._ring_begin(dial.panel_index):
            return
        self._cancel_grace()
        self._dial_drag = _DialDrag(q0=dial.q, theta=math.atan2(y, x))
        self._update_dial_label(dial)

    def _on_dial_move(self, e: ScenePointerEventArguments) -> None:
        if self._dial_drag is None:
            return
        self._track_pointer(e)

    async def _on_dial_up(self, e: ScenePointerEventArguments) -> None:
        dial = self._dial
        if self._dial_drag is None or dial is None:
            return
        self._track_pointer(e)
        self._dial_drag = None
        with batch_scene(self.scene):
            if dial.ticks is not None:
                dial.ticks.rotate(0.0, 0.0, dial.q)
            self._update_dial_label(dial)
        if self._ring_end is not None:
            await self._ring_end()
        self._settle_hover()

    def _track_pointer(self, e: ScenePointerEventArguments) -> None:
        """Accumulate the pointer's angle around the axis across the ±π seam."""
        drag, dial = self._dial_drag, self._dial
        x, y = e.x, e.y
        if drag is None or dial is None or x is None or y is None:
            return
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        if x * x + y * y < (_DEAD_ZONE * dial.radius) ** 2:
            return
        theta = math.atan2(y, x)
        d = theta - drag.theta
        if d > math.pi:
            d -= 2 * math.pi
        elif d < -math.pi:
            d += 2 * math.pi
        drag.theta = theta
        drag.acc += d
        self._apply_dial_drag()

    def _apply_dial_drag(self) -> None:
        """Snap the accumulated change to the step, clamp it to the travel, and send it on."""
        drag, dial = self._dial_drag, self._dial
        if drag is None or dial is None:
            return
        step = math.radians(self.snap.joint_deg)
        q = drag.q0 + round(drag.acc / step) * step
        if dial.lo is not None and dial.hi is not None:
            q = min(dial.hi, max(dial.lo, q))
        if q == dial.q:
            return
        with batch_scene(self.scene):
            self._place_knob(dial, q)
        if self._ring_target is not None:
            panel_index, deg = urdf_to_panel(dial.urdf_index, q)
            self._ring_target(panel_index, deg)
