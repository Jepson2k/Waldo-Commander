"""Keep-out shape editing in the 3D viewer.

Right-click empty space to place a Box/Sphere/Cylinder keep-out at the
clicked point; right-click a program-layer shape to edit its numbers, drag
it with translate controls, or delete it. Every mutation goes through
``commander.scene.shapes`` — the same request/readback path a program's
``set_shapes`` uses — so the backend push, draft styling and program
recording all apply unchanged. Installation-layer shapes come from the
backend's robot config and are not editable here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import fields
from typing import Any

import waldoctl
from nicegui import ui
from waldoctl.shapes import (
    Box,
    Capsule,
    Cone,
    Cylinder,
    Ellipsoid,
    Plane,
    Shape,
    Sphere,
    param_names,
)

_KINDS: dict[str, type] = {
    c.__name__.lower(): c
    for c in (Box, Sphere, Cylinder, Capsule, Cone, Ellipsoid, Plane)
}

logger = logging.getLogger(__name__)

# Kinds offered for placement. The edit dialog handles every kind a program
# may have created; these are just the ones worth a menu entry.
_PLACEABLE = ("box", "sphere", "cylinder")

# Reasonable birth sizes (m) so a fresh shape is visible and grabbable.
_DEFAULT_DIMS: dict[str, dict[str, float]] = {
    "box": {"x": 0.1, "y": 0.1, "z": 0.1},
    "sphere": {"radius": 0.05},
    "cylinder": {"radius": 0.05, "length": 0.1},
}


class ShapeEditingMixin:
    """Mixin providing keep-out shape editing for UrdfScene."""

    # Attributes from UrdfScene
    scene: Any
    context_menu: Any
    _shape_objects: dict[str, Any]

    def _init_shape_editing(self) -> None:
        self._shape_move_active: str | None = None

    # ------------------------------------------------------------------
    # Shape access (program layer only)
    # ------------------------------------------------------------------

    @staticmethod
    def _shape_handle():
        return waldoctl.commander.scene

    def _program_shape(self, name: str) -> Shape | None:
        handle = self._shape_handle()
        if handle is None:
            return None
        for s in handle.shapes:
            if s.name == name:
                return s
        return None

    def _shape_hit_name(self, hits) -> str | None:
        """The clicked program-layer shape's name, if any."""
        for h in hits:
            obj = getattr(h, "object_name", "") or ""
            if obj.startswith("shape:"):
                return obj.split("shape:", 1)[1]
        return None

    def _draft_hit_name(self, hits) -> str | None:
        """The clicked proposed-installation shape's name, if any."""
        for h in hits:
            obj = getattr(h, "object_name", "") or ""
            if obj.startswith("draft:"):
                return obj.split("draft:", 1)[1]
        return None

    def _fresh_shape_name(self, kind: str) -> str:
        handle = self._shape_handle()
        taken = set()
        if handle is not None:
            taken = {s.name for s in handle.shapes}
            taken |= {s.name for s in handle.installation}
        n = 1
        while f"{kind}-{n}" in taken:
            n += 1
        return f"{kind}-{n}"

    # ------------------------------------------------------------------
    # Context-menu contributions (called from _populate_context_menu)
    # ------------------------------------------------------------------

    def _populate_shape_menu(self, shape_name: str) -> None:
        """Menu items for a right-clicked program-layer shape."""
        shape = self._program_shape(shape_name)
        if shape is None:
            return
        ui.item(f"Keep-out '{shape_name}'").classes("font-bold text-sm")
        ui.separator()
        ui.menu_item(
            "Edit Keep-out...",
            on_click=lambda s=shape: self._show_shape_dialog(shape=s),
        )
        if self._shape_move_active == shape_name:
            ui.menu_item("Stop Moving", on_click=self._end_shape_move)
        elif shape.kind != "plane":
            # A plane renders at its surface, not at its pose — dragging
            # the slab would not move what the checker enforces.
            ui.menu_item(
                "Move (drag arrows)",
                on_click=lambda n=shape_name: self._start_shape_move(n),
            )
        ui.menu_item(
            "Delete Keep-out",
            on_click=lambda n=shape_name: self._delete_shape(n),
        )
        ui.separator()
        ui.menu_item(
            "Propose as Installation",
            on_click=lambda n=shape_name: self._propose_installation(n),
        ).mark("shape-menu-propose")

    def _populate_draft_menu(self, shape_name: str) -> None:
        """Menu items for a right-clicked proposed-installation shape."""
        ui.item(f"Proposed installation '{shape_name}'").classes("font-bold text-sm")
        ui.item("Not enforced until it is in the robot config").classes(
            "text-xs opacity-70"
        )
        ui.separator()
        ui.menu_item(
            "Export Installation TOML...",
            on_click=self._show_installation_toml_dialog,
        )
        ui.menu_item(
            "Back to Program Layer",
            on_click=lambda n=shape_name: self._withdraw_proposal(n),
        )
        ui.menu_item(
            "Discard Proposal",
            on_click=lambda n=shape_name: self._discard_proposal(n),
        )

    def _populate_installation_menu(self) -> None:
        """Empty-space items for a pending installation proposal."""
        handle = self._shape_handle()
        if handle is None or not handle.installation_draft:
            return
        ui.separator()
        n = len(handle.installation_draft)
        ui.item(f"Installation Proposal ({n})").classes("font-bold text-sm")
        ui.menu_item(
            "Export Installation TOML...",
            on_click=self._show_installation_toml_dialog,
        ).mark("installation-menu-export")
        ui.menu_item(
            "Discard Proposal",
            on_click=lambda: self._discard_proposal(None),
        )

    def _propose_installation(self, name: str) -> None:
        handle = self._shape_handle()
        if handle is None:
            return
        try:
            handle.propose_installation([name])
        except ValueError as err:
            logger.warning("Proposal refused: %s", err)

    def _withdraw_proposal(self, name: str) -> None:
        """A proposal returns to the program layer as the keep-out it was."""
        handle = self._shape_handle()
        if handle is None:
            return
        shape = next((s for s in handle.installation_draft if s.name == name), None)
        if shape is None:
            return
        handle.discard_installation_draft([name])
        try:
            handle.shapes = [*handle.shapes, shape]
        except ValueError as err:
            logger.warning("Keep-out rejected: %s", err)

    def _discard_proposal(self, name: str | None) -> None:
        handle = self._shape_handle()
        if handle is not None:
            handle.discard_installation_draft(None if name is None else [name])

    def _show_installation_toml_dialog(self) -> None:
        """The proposal as the robot config's ``[[installation_shapes]]``
        TOML, to copy or save: installation authoring is config authoring."""
        from waldo_commander.constants import default_program_dir
        from waldo_commander.services.world_files import installation_toml

        handle = self._shape_handle()
        if handle is None:
            return
        text = installation_toml(handle.installation_draft)
        with (
            ui.context.client.content,
            ui.dialog() as dialog,
            ui.card().classes("w-[36rem] max-w-full").mark("installation-toml-dialog"),
        ):
            ui.label("Installation TOML").classes("text-lg font-bold")
            ui.label(
                "Paste into the robot config's [[installation_shapes]] section; "
                "the backend enforces it from its next start, and the proposal "
                "clears itself once readback shows it."
            ).classes("text-sm opacity-80")
            ui.code(text, language="toml").classes("w-full max-h-96 overflow-auto")
            saved = ui.label().classes("text-sm").mark("installation-toml-saved")

            def save() -> None:
                path = default_program_dir() / "installation_shapes.toml"
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                except OSError as err:
                    saved.set_text(f"Save failed: {err}")
                    return
                saved.set_text(f"Saved to {path}")

            def dismiss() -> None:
                dialog.close()
                if not dialog.is_deleted:
                    dialog.delete()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Save to programs folder", on_click=save).props("flat").mark(
                    "installation-toml-save"
                )
                ui.button("Close", on_click=dismiss).props("unelevated")
        dialog.on("hide", lambda: dialog.is_deleted or dialog.delete())
        dialog.open()

    def _populate_shape_add_menu(self, click_point: tuple[float, float, float]) -> None:
        """'Add keep-out here' items for a right-click on empty space."""
        if self._shape_handle() is None:
            return
        ui.separator()
        ui.item("Add Keep-out").classes("font-bold text-sm")
        for kind in _PLACEABLE:
            ui.menu_item(
                f"{kind.capitalize()} Here...",
                on_click=lambda k=kind, p=click_point: self._show_shape_dialog(
                    kind=k, at=p
                ),
            )

    # ------------------------------------------------------------------
    # Add / edit dialog
    # ------------------------------------------------------------------

    def _show_shape_dialog(
        self,
        kind: str | None = None,
        shape: Shape | None = None,
        at: tuple[float, float, float] | None = None,
    ) -> None:
        """One dialog for both placing (``kind`` + ``at``) and editing
        (``shape``). Values display in mm / degrees; shapes store m / rad."""
        editing = shape is not None
        if editing:
            kind = shape.kind
        assert kind is not None
        cls = _KINDS[kind]
        dims_names = param_names(cls)

        if editing:
            pose = shape.pose
            dims = {p: getattr(shape, p) for p in dims_names}
            name0 = shape.name
            collision0 = shape.collision
            margin0 = shape.margin
        else:
            x, y, z = at if at is not None else (0.3, 0.0, 0.0)
            # Sit the birth shape on the clicked surface, not half inside it.
            d = _DEFAULT_DIMS[kind]
            lift = d.get("z", 0.0) / 2 if kind == "box" else d["radius"]
            if kind == "cylinder":
                lift = d["length"] / 2
            pose = (x, y, z + lift, 0.0, 0.0, 0.0)
            dims = dict(_DEFAULT_DIMS[kind])
            name0 = self._fresh_shape_name(kind)
            collision0 = True
            margin0 = None

        # Parent to the page, not the caller's slot: opened from a context
        # menu item, the enclosing slot is the menu itself, and the menu's
        # hide handler clears its children — taking the dialog with it.
        with (
            ui.context.client.content,
            ui.dialog() as dialog,
            ui.card().classes("w-80"),
        ):
            ui.label(f"{'Edit' if editing else 'Add'} {kind} keep-out").classes(
                "text-lg font-bold"
            )
            name_in = (
                ui.input("Name", value=name0)
                .classes("w-full")
                .mark("shape-dialog-name")
            )
            with ui.row().classes("gap-1 w-full"):
                pos_in = [
                    ui.number(label, value=round(pose[i] * 1000, 1), suffix="mm")
                    .classes("flex-1")
                    .mark(f"shape-dialog-pos-{label.lower()}")
                    for i, label in enumerate(("X", "Y", "Z"))
                ]
            with ui.row().classes("gap-1 w-full"):
                rot_in = [
                    ui.number(
                        label, value=round(math.degrees(pose[3 + i]), 1), suffix="°"
                    ).classes("flex-1")
                    for i, label in enumerate(("Rx", "Ry", "Rz"))
                ]
            dim_in: dict[str, Any] = {}
            with ui.row().classes("gap-1 w-full"):
                for p in dims_names:
                    if kind == "plane" and p != "offset":
                        # Plane normals are directions, not lengths.
                        dim_in[p] = ui.number(p, value=dims[p]).classes("flex-1")
                    else:
                        dim_in[p] = (
                            ui.number(p, value=round(dims[p] * 1000, 1), suffix="mm")
                            .classes("flex-1")
                            .mark(f"shape-dialog-dim-{p}")
                        )
            collision_in = ui.switch(
                "Collision (off = visual marker)", value=collision0
            )
            margin_in = ui.number(
                "Clearance margin (blank = robot default)",
                value=None if margin0 is None else round(margin0 * 1000, 1),
                suffix="mm",
            ).classes("w-full")

            def save() -> None:
                handle = self._shape_handle()
                if handle is None:
                    dialog.close()
                    return
                try:
                    params = {}
                    for p in dims_names:
                        v = float(dim_in[p].value)
                        params[p] = (
                            v if (kind == "plane" and p != "offset") else v / 1000
                        )
                    margin_v = margin_in.value
                    new = cls(
                        name=str(name_in.value).strip() or name0,
                        pose=tuple(
                            [float(w.value) / 1000 for w in pos_in]
                            + [math.radians(float(w.value)) for w in rot_in]
                        ),
                        collision=bool(collision_in.value),
                        margin=None
                        if margin_v in (None, "")
                        else float(margin_v) / 1000,
                        # The dialog edits geometry; a body stays a body.
                        physics=None if shape is None else shape.physics,
                        **params,
                    )
                except (TypeError, ValueError) as err:
                    ui.notify(f"Keep-out rejected: {err}", color="warning")
                    return
                shapes = [s for s in handle.shapes if s.name not in (name0, new.name)]
                shapes.append(new)
                try:
                    handle.shapes = shapes
                except ValueError as err:
                    ui.notify(f"Keep-out rejected: {err}", color="warning")
                    return
                dismiss()

            def dismiss() -> None:
                # Synchronous removal: the hide event needs a client round
                # trip, and a lingering dialog's identically-marked save
                # button would shadow the next dialog's until it lands.
                dialog.close()
                if not dialog.is_deleted:
                    dialog.delete()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dismiss).props("flat")
                ui.button("Save", on_click=save).props("unelevated").mark(
                    "shape-dialog-save"
                )
        # ESC / backdrop dismissal comes back as a hide event.
        dialog.on("hide", lambda: dialog.is_deleted or dialog.delete())
        dialog.open()

    def _delete_shape(self, name: str) -> None:
        handle = self._shape_handle()
        if handle is None:
            return
        if self._shape_move_active == name:
            self._end_shape_move()

        with ui.context.client.content, ui.dialog() as dialog, ui.card():
            ui.label(f"Delete keep-out '{name}'?")

            def dismiss() -> None:
                dialog.close()
                if not dialog.is_deleted:
                    dialog.delete()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dismiss).props("flat")

                def confirm() -> None:
                    handle.shapes = [s for s in handle.shapes if s.name != name]
                    dismiss()

                ui.button("Delete", color="negative", on_click=confirm).props(
                    "unelevated"
                ).mark("shape-delete-confirm")
        dialog.on("hide", lambda: dialog.is_deleted or dialog.delete())
        dialog.open()

    # ------------------------------------------------------------------
    # Drag-to-move
    # ------------------------------------------------------------------

    def _start_shape_move(self, name: str) -> None:
        obj = self._shape_objects.get(f"shape:{name}")
        if obj is None:
            return
        if self._shape_move_active and self._shape_move_active != name:
            self._end_shape_move()
        obj.enable_transform_controls(mode="translate", size=0.5)
        self._shape_move_active = name

    def _end_shape_move(self) -> None:
        name = self._shape_move_active
        self._shape_move_active = None
        if name is None:
            return
        obj = self._shape_objects.get(f"shape:{name}")
        if obj is not None:
            try:
                obj.disable_transform_controls()
            except Exception:
                logger.debug("transform-control teardown raced a re-render")

    def _on_shape_transform(self, e) -> None:
        """A dragged keep-out landed: write the new position through the
        request path. The re-render is a diff, so the dragged object — and
        the controls on it — survive for the next nudge."""
        if getattr(e, "type", "") != "transform_end":
            return
        object_name = getattr(e, "object_name", "") or ""
        name = object_name.split("shape:", 1)[1]
        if name != self._shape_move_active:
            return
        handle = self._shape_handle()
        shape = self._program_shape(name)
        if handle is None or shape is None or e.x is None:
            return
        moved = _KINDS[shape.kind](
            **{
                f.name: getattr(shape, f.name)
                for f in fields(type(shape))
                if f.name != "pose"
            },
            pose=(float(e.x), float(e.y), float(e.z), *shape.pose[3:]),
        )
        handle.shapes = [moved if s.name == name else s for s in handle.shapes]
