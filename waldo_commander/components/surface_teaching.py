"""Teach a world-fixed surface operation using the existing preview robot."""

from __future__ import annotations

import ast
import math

import numpy as np
from nicegui import ui
from scipy.spatial.transform import Rotation
from waldoctl.setup import Frame, Pose, SetupSnapshot

from waldo_commander import scene3d as s3d
from waldo_commander.common.theme import hex_of
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.python_source import in_async_scope, missing_statements
from waldo_commander.services.urdf_scene.shape_drawing_mixin import surface_frame
from waldo_commander.setup import export_snapshot
from waldo_commander.state import ui_state

_SNAPSHOT_IMPORT = "from waldoctl.setup import SetupSnapshot"


class SurfaceTeaching:
    def __init__(self, view, surface, revision):
        self.view = view
        self.revision = revision
        self.cursor = motion_recorder.insertion_line()
        self.origin = np.array(surface["origin"], dtype=float)
        self.normal = np.array(surface["normal"], dtype=float)
        self.valid = False
        self.snapshot = None
        self.group = None
        self.card = None
        self.flipped = False
        view.object_history.guard(revision)
        if not view._ensure_ik_solver():
            raise ValueError("Surface teaching needs a kinematics model.")
        self.control_context = view._drag_context_now()
        view.cancel_target_edit()
        view._select_objects([])
        view.inspection.clear_cursor()
        view.enter_editing_mode(view._get_robot_angles_rad())
        view._teaching = self
        fk = view._ik_solver.forward_kinematics(view._editing_angles)
        self.frame = surface_frame(
            self.normal, Rotation.from_euler("XYZ", fk[3:6]).as_matrix()[:, 0]
        )
        with view.scene.client, view._scene_wrapper:
            self.card = (
                ui.column()
                .classes(
                    "wc-surface-teaching overlay-card absolute z-40 p-3 pointer-events-auto"
                )
                .style("width:280px;overflow:auto;gap:8px")
                .mark("surface-teaching")
            )
            self.card.on("keydown.escape", view.cancel_target_edit)
            with self.card:
                with ui.row().classes("w-full items-center no-wrap"):
                    ui.label("Teach on surface").classes("font-medium flex-1")
                    ui.button(icon="close", on_click=view.cancel_target_edit).props(
                        "flat round dense color=wc-text"
                    ).mark("surface-cancel").tooltip("Cancel · Esc")
                ui.label(f"{surface['label']} · world-fixed").classes(
                    "text-xs text-wc-text-muted"
                )
                self.direction = (
                    ui.label("Tool +Z points toward the surface.")
                    .classes("text-sm")
                    .mark("surface-direction")
                )
                self.standoff = (
                    ui.number("Stand-off · mm", value=30, format="%.3f")
                    .props("dense outlined")
                    .classes("w-full")
                    .mark("surface-standoff")
                )
                self.twist = (
                    ui.number("Twist about normal · °", value=0, format="%.3f")
                    .props("dense outlined")
                    .classes("w-full")
                    .mark("surface-twist")
                )
                ui.button(
                    "Flip tool approach", icon="flip_camera_android", on_click=self.flip
                ).props("flat dense no-caps color=wc-text").mark("surface-flip")
                self.sequence = (
                    ui.checkbox(
                        "Add approach and retract", value=False, on_change=self.update
                    )
                    .props("dense")
                    .mark("surface-sequence")
                )
                self.clearance = (
                    ui.number("Approach / retract · mm", value=30, format="%.3f")
                    .props("dense outlined")
                    .classes("w-full")
                    .mark("surface-clearance")
                )
                self.clearance.set_visibility(False)
                self.status = ui.label().classes("text-sm").mark("surface-status")
                self.coordinates = (
                    ui.label()
                    .classes("text-xs text-wc-text-muted font-mono")
                    .mark("surface-coordinates")
                )
                ui.label(
                    "Dot: surface · axes: requested TCP · robot: last valid pose"
                ).classes("text-xs text-wc-text-muted")
                self.insert = (
                    ui.button("Insert target", icon="add", on_click=self.commit)
                    .props("unelevated no-caps color=wc-action text-color=wc-on-bright")
                    .classes("w-full")
                    .mark("surface-insert")
                )
                for field in (self.standoff, self.twist, self.clearance):
                    field.on("blur", self.update)
                    field.on("keydown.enter", self.update)
        view._push_interaction()
        self.update()

    def flip(self):
        self.flipped = not self.flipped
        self.direction.set_text(
            "Tool +Z points away from the surface."
            if self.flipped
            else "Tool +Z points toward the surface."
        )
        self.update()

    def check_context(self):
        if self.view._teaching is not self:
            return False
        if (
            self.view.object_history.context() != self.revision
            or self.view._drag_context_now() != self.control_context
            or self.view._edit_document != self.view._edit_document_key()
        ):
            self.view.cancel_target_edit()
            with self.view.scene.client:
                ui.notify(
                    "Surface preview cancelled: program, world, tool or control changed.",
                    color="info",
                )
            return False
        return self.view._teaching is self

    def update(self, _=None):
        self.valid = False
        if not self.check_context():
            return
        self.insert.disable()
        self.clearance.set_visibility(bool(self.sequence.value))
        self.insert.set_text(
            "Insert sequence" if self.sequence.value else "Insert target"
        )
        try:
            distance, twist, clearance = (
                float(f.value) for f in (self.standoff, self.twist, self.clearance)
            )
            if (
                not all(math.isfinite(v) for v in (distance, twist, clearance))
                or distance < 0
                or clearance < 0
            ):
                raise ValueError(
                    "Enter finite distances of zero or more and a finite twist."
                )
            if self.sequence.value and clearance <= 0:
                raise ValueError(
                    "Approach / retract distance must be greater than zero."
                )
        except (TypeError, ValueError) as error:
            self.status.set_text(str(error))
            if self.group:
                self.group.visible(False)
            return
        normal_rotation = Rotation.from_rotvec(
            self.normal * math.radians(twist)
        ).as_matrix()
        tcp_rotation = (
            normal_rotation
            @ self.frame
            @ np.diag([1, 1 if self.flipped else -1, 1 if self.flipped else -1])
        )
        point = self.origin + self.normal * distance / 1000
        matrix = np.eye(4)
        matrix[:3, :3] = tcp_rotation
        matrix[:3, 3] = point * 1000
        pose = Pose.from_matrix(matrix)
        frame_matrix = np.eye(4)
        frame_matrix[:3, :3] = self.frame
        frame_matrix[:3, 3] = self.origin * 1000
        snapshot = SetupSnapshot(
            frames={"surface": Frame(Pose.from_matrix(frame_matrix).values)}
        )
        for name, offset in (
            ("work", 0),
            ("approach", clearance),
            ("retract", clearance),
        ):
            candidate = matrix.copy()
            candidate[:3, 3] += self.normal * offset
            snapshot = snapshot.with_pose(
                name, snapshot.relative_pose(Pose.from_matrix(candidate), "surface")
            )
        self.snapshot = snapshot
        self.coordinates.set_text(
            "TCP · " + " / ".join(f"{v:.1f}" for v in pose.values[:3]) + " mm"
        )
        view = self.view
        result = view._ik_solver.solve(
            point, view._editing_angles, np.radians(pose.values[3:])
        )
        if result.success and self.sequence.value:
            approach = snapshot.resolve("approach").values
            extra = view._ik_solver.solve(
                np.array(approach[:3]) / 1000, result.angles, np.radians(approach[3:])
            )
            if not extra.success:
                self.status.set_text(
                    "Approach is unreachable · reduce the distance or adjust the pose."
                )
            else:
                self.valid = True
        elif result.success:
            self.valid = True
        else:
            self.status.set_text(
                "Requested pose is unreachable · last valid robot pose retained."
            )
        if result.success:
            view.set_editing_angles(result.angles)
            view._refresh_editing_pose()
        if self.valid:
            self.status.set_text(
                "Poses reachable · preview only"
                if self.sequence.value
                else "Pose reachable · preview only"
            )
            self.insert.enable()
        self._draw(point, tcp_rotation, clearance / 1000)

    def _draw(self, point, rotation, clearance):
        if self.group:
            self.group.delete()
        with self.view.scene:
            self.group = s3d.group().with_name("teaching:preview")
            with self.group:
                s3d.sphere(0.004).move(*self.origin).material(hex_of("text"))
                self._arrow(self.origin, self.normal, 0.065, "text-muted")
                s3d.line(self.origin.tolist(), point.tolist()).material(
                    hex_of("text-muted")
                )
                with s3d.group().move(*point).rotate_R(rotation.tolist()):
                    s3d.sphere(0.006, wireframe=True).material(
                        hex_of("action" if self.valid else "error")
                    )
                    for axis, token in enumerate(("axis-x", "axis-y", "axis-z")):
                        self._arrow(np.zeros(3), np.eye(3)[axis], 0.045, token)
                if self.sequence.value:
                    end = point + self.normal * clearance
                    s3d.line(point.tolist(), end.tolist()).material(hex_of("action"))
                    s3d.sphere(0.004, wireframe=True).move(*end).material(
                        hex_of("action")
                    )

    @staticmethod
    def _arrow(origin, axis, length, token):
        rotation, _ = Rotation.align_vectors([axis], [[0, 1, 0]])
        with s3d.group().move(*origin).rotate_R(rotation.as_matrix().tolist()):
            s3d.cylinder(0.001, 0.001, length - 0.01).move(
                0, (length - 0.01) / 2, 0
            ).material(hex_of(token))
            s3d.cylinder(0, 0.004, 0.01).move(0, length - 0.005, 0).material(
                hex_of(token)
            )

    def commit(self):
        self.update()
        if not self.valid or self.snapshot is None:
            return
        try:
            self.view.object_history.guard(self.revision)
            if not self.check_context():
                return
            editor = ui_state.active_textarea
            if editor is None or editor.client is not self.view.scene.client:
                raise ValueError("Open a program before inserting the operation.")
            payload = self.snapshot.to_dict()
            for section in ("frames", "poses"):
                for entry in payload[section].values():
                    entry["values"] = [round(v, 6) + 0.0 for v in entry["values"]]
            if not self.sequence.value:
                payload["poses"] = {"work": payload["poses"]["work"]}
            snapshot = SetupSnapshot.from_dict(payload)
            motions = (
                [("move_j", "approach"), ("move_l", "work"), ("move_l", "retract")]
                if self.sequence.value
                else [("move_j", "work")]
            )

            def compose(original: str, line: int) -> str:
                tree = ast.parse(original)
                names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                names.update(n.arg for n in ast.walk(tree) if isinstance(n, ast.arg))
                name = "surface_task"
                index = 2
                while name in names:
                    name, index = f"surface_task_{index}", index + 1
                snippet = export_snapshot(
                    snapshot,
                    variable=name,
                    imports=bool(missing_statements(original, [_SNAPSHOT_IMPORT])),
                )
                awaited = "await " if in_async_scope(original, line) else ""
                calls = [
                    f'{awaited}rbt.{method}(pose={name}.resolve("{pose}").as_list())'
                    for method, pose in motions
                ]
                return snippet + "\n".join(calls) + "\n"

            self.view.cancel_target_edit()
            motion_recorder.insert_scene_code(compose, cursor=self.cursor)
        except (ValueError, SyntaxError) as error:
            if self.view._teaching is self:
                self.status.set_text(str(error))

    def close(self):
        self.valid = False
        if self.group:
            self.group.delete()
        if self.card:
            self.card.delete()
