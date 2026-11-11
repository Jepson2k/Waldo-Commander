"""New keep-outs on the floor or a resolved box face: placed at a default
size, or drawn there with the pointer."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation
from waldoctl.shapes import SHAPE_PREFIX, Shape, pose_matrix

from waldo_commander.scene3d.interaction import finite
from .shape_editing_mixin import ShapeEditingMixin, _KINDS, _PLACEABLE

# Reasonable birth sizes (m) so a fresh shape is visible and grabbable.
_DEFAULT_DIMS: dict[str, dict[str, float]] = {
    "box": {"x": 0.1, "y": 0.1, "z": 0.1},
    "sphere": {"radius": 0.05},
    "cylinder": {"radius": 0.05, "length": 0.1},
}


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


def _plane_rotation(normal: np.ndarray) -> np.ndarray:
    """The frame a shape made on a plane with *normal* stands in."""
    return surface_frame(normal, np.eye(3)[0 if abs(normal[0]) < 0.9 else 1])


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
        self._keepout_card = None

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
            "id": key,
            "origin": origin.tolist(),
            "normal": normal.tolist(),
            "label": f"Face of {shape.name}",
        }

    async def _add_shape(
        self, label: str, shape: Shape, context, *, installation: bool, resize=False
    ) -> bool:
        """Commit the new *shape*, selected (in its resize handles with
        *resize*), or into the installation proposal; whether it was taken."""
        handle = self._shape_handle()
        if installation:
            return await self._commit_objects(
                label,
                list(handle.shapes),
                context,
                draft=(*handle.installation_draft, shape),
            )
        if not await self._commit_objects(label, [*handle.shapes, shape], context):
            return False
        if resize:
            self._object_mode = "resize"
        self._select_objects([f"{SHAPE_PREFIX}{shape.name}"])
        return True

    async def _place_shape(self, kind: str, plane, *, installation=False) -> bool:
        """A default-sized *kind* standing on *plane* at its origin; whether
        it was added."""
        if self._shape_handle() is None or kind not in _PLACEABLE:
            return False
        dims = dict(_DEFAULT_DIMS[kind])
        lift = (
            dims["z"] / 2
            if kind == "box"
            else dims["length"] / 2
            if kind == "cylinder"
            else dims["radius"]
        )
        normal = np.array(plane["normal"], dtype=float)
        rotation = _plane_rotation(normal)
        # To the millimetre, as a position typed into the program would be.
        center = [round(float(v), 3) for v in np.array(plane["origin"]) + normal * lift]
        # Upright, its angles are written as plain zeros.
        turn = (
            (0.0, 0.0, 0.0)
            if np.allclose(rotation, np.eye(3))
            else tuple(Rotation.from_matrix(rotation).as_euler("xyz").tolist())
        )
        shape = _KINDS[kind](
            name=self._fresh_shape_name(kind), pose=(*center, *turn), **dims
        )
        return await self._add_shape(
            "Create object",
            shape,
            self.object_history.context(),
            installation=installation,
        )

    def _start_shape_drawing(self, kind, plane, context, *, installation=False):
        """Arm a drawing of *kind* on *plane*; its session. ValueError says
        why the world cannot take one now."""
        if kind not in _PLACEABLE:
            raise ValueError(f"A {kind} cannot be drawn")
        self.object_history.guard(context)
        # Taking control for the drawing is no change of control to end it.
        self._admitted()
        self._cancel_shape_drawing()
        self._select_objects([])
        self.cancel_target_edit()
        self.inspection.clear_cursor()
        self.suspend_hover()
        rotation = _plane_rotation(np.array(plane["normal"], dtype=float))
        self._drawing_session += 1
        self._drawing = {
            "session": self._drawing_session,
            "primitive": kind,
            "origin": plane["origin"],
            "quaternion": Rotation.from_matrix(rotation).as_quat().tolist(),
            "context": context,
            "installation": installation,
        }
        self._push_interaction()
        self._drawing_changed()
        return self._drawing_session

    def _shape_draw_state(self):
        if self._drawing is None:
            return None
        return {
            k: self._drawing[k]
            for k in ("session", "primitive", "origin", "quaternion")
        }

    def _drawing_changed(self) -> None:
        if self._keepout_card is not None:
            self._keepout_card.follow()

    def _on_draw_phase(self, event) -> None:
        args = event.args
        if (
            self._drawing is not None
            and isinstance(args, dict)
            and args.get("session") == self._drawing["session"]
            and self._keepout_card is not None
        ):
            self._keepout_card.show_phase(args)

    def _cancel_shape_drawing(self, event=None):
        if self._drawing is None:
            return
        if event is not None and event.args.get("session") != self._drawing["session"]:
            return
        self._drawing = None
        self.scene.gestures.abort("draw")
        self.resume_hover()
        self._push_interaction()
        self._drawing_changed()

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
            if not await self._add_shape(
                "Draw object",
                shape,
                state["context"],
                installation=state["installation"],
                resize=True,
            ):
                return
            card = self._keepout_card
            if card is not None and card.session == state["session"]:
                card.close()

        self._spawn(commit())
