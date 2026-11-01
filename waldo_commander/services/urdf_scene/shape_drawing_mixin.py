"""Transient primitive drawing on the floor or a resolved box face."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from nicegui import ui
from scipy.spatial.transform import Rotation
from waldoctl.shapes import SHAPE_PREFIX, pose_matrix

from waldo_commander.scene3d.interaction import finite
from .shape_editing_mixin import ShapeEditingMixin, _KINDS, _PLACEABLE


def surface_frame(normal: np.ndarray, preferred: np.ndarray) -> np.ndarray:
    """A rotation whose z is the unit *normal* and whose x is *preferred*
    laid into the surface, or the axis least along the normal when
    *preferred* points along it."""
    tangent = preferred - normal * np.dot(preferred, normal)
    if np.linalg.norm(tangent) < 1e-6:
        tangent = np.eye(3)[int(np.argmin(np.abs(normal)))]
        tangent = tangent - normal * np.dot(tangent, normal)
    tangent = tangent / np.linalg.norm(tangent)
    return np.column_stack((tangent, np.cross(normal, tangent), normal))


class ShapeDrawingMixin(ShapeEditingMixin):
    _context_surface: dict | None
    _drawing: dict | None
    _drawing_session: int
    _selected_objects: Any
    _object_shape: Any
    cancel_target_edit: Any
    suspend_hover: Any
    resume_hover: Any
    inspection: Any
    _admitted: Any

    def _init_shape_drawing(self):
        self._context_surface = None
        self._drawing = None
        self._drawing_session = 0

    def _resolve_draw_surface(self, args):
        raw = args.get("surface")
        self._context_surface = None
        if not isinstance(raw, dict):
            return
        key = raw.get("id")
        if not isinstance(key, str):
            return
        shape = self._object_shape(key)
        if shape is None or shape.kind != "box" or shape.attachment is not None:
            return
        point = finite(raw, "x", "y", "z")
        if point is None:
            return
        transform = pose_matrix(shape.pose)
        local = transform[:3, :3].T @ (np.array(point) - transform[:3, 3])
        half = np.array([shape.x, shape.y, shape.z]) / 2
        if np.any(np.abs(local) > half + 0.002):
            return
        axis = int(np.argmin(np.abs(np.abs(local) - half)))
        if abs(abs(local[axis]) - half[axis]) > 0.002:
            return
        normal = transform[:3, axis] * (1 if local[axis] >= 0 else -1)
        local[axis] = math.copysign(half[axis], local[axis])
        origin = transform[:3, 3] + transform[:3, :3] @ local
        self._context_surface = {
            "origin": origin.tolist(),
            "normal": normal.tolist(),
            "label": f"Face of {shape.name}",
        }

    def _populate_draw_menu(self, point, surface=None):
        plane = surface or {
            "origin": list(point),
            "normal": [0, 0, 1],
            "label": "Floor",
        }
        context = self.object_history.context()
        ui.separator()
        ui.item(f"Draw keep-out · {plane['label']}").classes("font-bold text-sm")
        for kind in _PLACEABLE:
            ui.menu_item(
                f"Draw {kind.capitalize()}…",
                on_click=lambda k=kind, p=plane: self._start_shape_drawing(
                    k, p, context
                ),
            ).mark(f"scene-draw-{kind}")

    def _start_shape_drawing(self, kind, plane, context):
        if kind not in _PLACEABLE:
            return
        try:
            self.object_history.guard(context)
        except ValueError as error:
            self._notify(str(error), warning=True)
            return
        # Taking control for the drawing is no change of control to end it.
        self._admitted()
        self._cancel_shape_drawing()
        self._select_objects([])
        self.cancel_target_edit()
        self.inspection.clear_cursor()
        self.suspend_hover()
        normal = np.array(plane["normal"], dtype=float)
        rotation = surface_frame(normal, np.eye(3)[0 if abs(normal[0]) < 0.9 else 1])
        self._drawing_session += 1
        self._drawing = {
            "session": self._drawing_session,
            "primitive": kind,
            "origin": plane["origin"],
            "quaternion": Rotation.from_matrix(rotation).as_quat().tolist(),
            "label": plane["label"],
            "context": context,
        }
        self._push_interaction()

    def _shape_draw_state(self):
        if self._drawing is None:
            return None
        return {k: v for k, v in self._drawing.items() if k != "context"}

    def _cancel_shape_drawing(self, event=None):
        if self._drawing is None:
            return
        if event is not None and event.args.get("session") != self._drawing["session"]:
            return
        self._drawing = None
        self.scene.gestures.abort("draw")
        self.resume_hover()
        self._push_interaction()

    def check_drawing_context(self):
        if self._drawing and self._drawing["context"] != self.object_history.context():
            self._cancel_shape_drawing()

    def _draw_admit(self, gesture, args):
        state = self._drawing
        if state is None or args.get("session") != state["session"]:
            return False
        try:
            self.object_history.guard(state["context"])
        except ValueError:
            self._cancel_shape_drawing()
            return False
        gesture.data.update(state)
        return True

    def _draw_finish(self, gesture, args, aborted):
        state = self._drawing
        # An aborted stroke leaves the drawing armed for another; the browser
        # ends the drawing itself with draw_cancel.
        if state is None or aborted:
            return
        self._cancel_shape_drawing()
        if args is None or args.get("session") != state["session"]:
            return
        values = finite(args, "u", "v", "width", "depth", "height")
        if values is None or min(values[2:]) <= 0:
            return
        u, v, width, depth, height = values
        kind = state["primitive"]
        if kind != "box":
            depth = width
        if kind == "sphere":
            height = width
        rotation = Rotation.from_quat(state["quaternion"])
        center = np.array(state["origin"]) + rotation.apply([u, v, height / 2])
        params = (
            {"x": width, "y": depth, "z": height}
            if kind == "box"
            else {"radius": width / 2}
        )
        if kind == "cylinder":
            params["length"] = height
        shape = _KINDS[kind](
            name=self._fresh_shape_name(kind),
            pose=(*center.tolist(), *rotation.as_euler("xyz").tolist()),
            **params,
        )

        async def commit():
            if await self._commit_objects(
                "Draw object", [*self._shape_handle().shapes, shape], state["context"]
            ):
                self._object_mode = "resize"
                self._select_objects([f"{SHAPE_PREFIX}{shape.name}"])

        self._spawn(commit())
