"""Keep-out shape editing in the 3D viewer.

Right-click empty space to place a Box/Sphere/Cylinder keep-out at the
clicked point; right-click a program-layer shape to select it for editing,
drag it, attach it to the flange or delete it. Every mutation goes through
``commander.scene.shapes`` — the same request/readback path a program's
``set_shapes`` uses — so the backend push, draft styling and program
recording all apply unchanged. Installation-layer shapes come from the
backend's robot config and are not editable here.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, cast

import waldoctl
from nicegui import ui

from waldo_commander.scene3d.interaction import finite
from waldo_commander.services.urdf_scene.config import DRAFT_PREFIX
from waldo_commander.services.control_lease import require_browser_control
from waldo_commander.state import ui_state
from waldoctl.shapes import (
    SHAPE_PREFIX,
    Box,
    Capsule,
    Cone,
    Cylinder,
    Ellipsoid,
    Shape,
    Pose6,
    Sphere,
    param_names,
    pose_matrix,
)

_KINDS: dict[str, type] = {
    c.__name__.lower(): c for c in (Box, Sphere, Cylinder, Capsule, Cone, Ellipsoid)
}

logger = logging.getLogger(__name__)

# Kinds offered for placement; the inspector edits every kind a program may
# have created.
_PLACEABLE = ("box", "sphere", "cylinder")

# The resizable kinds, and the dimension a handle on each local axis changes;
# the browser draws its resize handles from the same table.
RESIZE_AXES: dict[str, tuple[str, str, str]] = {
    "box": ("x", "y", "z"),
    "sphere": ("radius", "radius", "radius"),
    "cylinder": ("radius", "radius", "length"),
}


def object_label(shape: Shape) -> str:
    """What the scene calls *shape*: the arm keeps out of a colliding one,
    and may reach into, or carry, one that does not collide."""
    if shape.attachment is not None:
        return "Flange attachment"
    return "Keep-out" if shape.collision else "Object"


# A released value this close to the authored one (m, rad) is the browser's
# rounding, not a move.
_UNMOVED = 1e-6

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
    _push_interaction: Any
    _commit_objects: Any
    _notify: Any
    _spawn: Any
    _select_objects: Any
    _object_mode: str
    _resize_centre: bool
    _locked_objects: set[str]
    object_history: Any
    _cancel_shape_drawing: Any
    _selected_objects: list[str]
    _hidden_objects: set[str]
    _refresh_object_inspector: Any

    def _init_shape_editing(self) -> None:
        self._shape_move_active: str | None = None
        self._shape_move_members: tuple[str, ...] = ()
        # Numbers each keep-out move, so a drag from an earlier one cannot land.
        self._shape_move_session = 0
        self._shape_move_document = None
        self._object_dialogs: list[tuple[Any, tuple]] = []
        # The inspector's open attachment form: (object key, detach).
        self._attachment_form: tuple[str, bool] | None = None
        self._attachment_revision: tuple = ()

    def _shape_locked(self, name: str) -> bool:
        return f"{SHAPE_PREFIX}{name}" in self._locked_objects

    def check_object_context(self) -> None:
        document = self.object_history.document()
        if self._shape_move_active and self._shape_move_document != document:
            self._end_shape_move()
        for dialog, context in self._object_dialogs[:]:
            if dialog.is_deleted:
                self._object_dialogs.remove((dialog, context))
            elif context != document:
                dialog.close()
                dialog.delete()
                self._object_dialogs.remove((dialog, context))

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

    @staticmethod
    def _hit_names(hits: list[str], prefix: str) -> Iterator[str]:
        """Names of the clicked objects drawn under *prefix*, nearest first."""
        for name in hits:
            if name.startswith(prefix):
                yield name[len(prefix) :]

    def _shape_hit_name(self, hits) -> str | None:
        """The clicked program-layer shape's name, if any."""
        return next(self._hit_names(hits, SHAPE_PREFIX), None)

    def _draft_hit_name(self, hits) -> str | None:
        """The clicked proposed-installation shape's name, if any."""
        return next(self._hit_names(hits, DRAFT_PREFIX), None)

    def _fresh_shape_name(self, stem: str, taken: set[str] | None = None) -> str:
        """``stem-n`` for the first n no object, nor anything in *taken*, uses."""
        handle = self._shape_handle()
        if taken is None:
            taken = set()
        if handle is not None:
            taken = taken | {
                s.name
                for s in (
                    *handle.shapes,
                    *handle.installation,
                    *handle.installation_draft,
                )
            }
        n = 1
        while f"{stem}-{n}" in taken:
            n += 1
        return f"{stem}-{n}"

    # ------------------------------------------------------------------
    # Context-menu contributions (called from _populate_context_menu)
    # ------------------------------------------------------------------

    def _populate_shape_menu(self, shape_name: str) -> None:
        """Menu items for a right-clicked program-layer shape."""
        shape = self._program_shape(shape_name)
        if shape is None:
            return
        locked = self._shape_locked(shape_name)
        ui.item(f"{object_label(shape)} '{shape_name}'").classes("font-bold text-sm")
        if shape.attachment is not None and not self._shape_handle().attachments_valid:
            ui.item("Reconciliation required before arm motion").classes(
                "text-wc-warning text-xs"
            )
        ui.separator()
        ui.menu_item(
            "Edit object",
            on_click=lambda s=shape: self._select_objects([f"{SHAPE_PREFIX}{s.name}"]),
        ).mark("shape-menu-edit").set_enabled(not locked)
        if shape.attachment is not None:
            ui.menu_item(
                "Reconcile attachment",
                on_click=lambda: self._open_attachment_form(shape_name),
            ).set_enabled(not locked)
            ui.menu_item(
                "Detach to world",
                on_click=lambda: self._open_attachment_form(shape_name, detach=True),
            ).set_enabled(not locked)
        else:
            ui.menu_item(
                "Attach to flange",
                on_click=lambda: self._open_attachment_form(shape_name),
            ).mark("shape-menu-attach").set_enabled(not locked)
        if self._shape_move_active == shape_name:
            ui.menu_item("Stop Moving", on_click=self._end_shape_move).mark(
                "shape-menu-stop-move"
            )
        else:
            ui.menu_item(
                "Move (drag arrows)",
                on_click=lambda n=shape_name: self._start_shape_move(n),
            ).mark("shape-menu-move").set_enabled(
                shape.attachment is None and not locked
            )
        ui.separator()
        ui.menu_item(
            "Propose as Installation",
            on_click=lambda n=shape_name: self._propose_installation(n),
        ).mark("shape-menu-propose").set_enabled(
            shape.attachment is None and not locked
        )
        ui.separator()
        ui.menu_item(
            "Delete",
            on_click=lambda n=shape_name: self._delete_shape(n),
        ).classes("text-wc-error").mark("shape-menu-delete").set_enabled(not locked)

    def _populate_draft_menu(self, shape_name: str) -> None:
        """Menu items for a right-clicked proposed-installation shape."""
        ui.item(f"Proposed installation '{shape_name}'").classes("font-bold text-sm")
        ui.item("Not enforced until it is in the robot config").classes(
            "text-xs text-wc-text-muted"
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

    async def _propose_installation(self, name: str) -> None:
        handle = self._shape_handle()
        if handle is None or self._shape_locked(name):
            return
        # The proposal deletes shape:<name> and redraws it as draft:<name>;
        # live transform controls on the old object would outlive it.
        self._end_shape_move()
        try:
            shapes, draft = handle.proposal_layers([name])
        except ValueError as err:
            self._notify(str(err), warning=True)
            return
        await self._commit_objects(
            "Propose as installation",
            shapes,
            self.object_history.context(),
            draft=draft,
        )

    async def _withdraw_proposal(self, name: str) -> None:
        """A proposal returns to the program layer as the keep-out it was."""
        handle = self._shape_handle()
        if handle is None:
            return
        try:
            shapes, draft = handle.withdrawal_layers(name)
        except ValueError as err:
            self._notify(str(err), warning=True)
            return
        await self._commit_objects(
            "Back to program layer",
            shapes,
            self.object_history.context(),
            draft=draft,
        )

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
            ui.card()
            .classes("task-dialog w-[36rem] max-w-full")
            .mark("installation-toml-dialog"),
        ):
            ui.label("Installation TOML").classes("text-lg font-bold")
            ui.label(
                "Paste into the robot config's [[installation_shapes]] section; "
                "the backend enforces it from its next start, and the proposal "
                "clears itself once readback shows it."
            ).classes("text-sm text-wc-text-muted")
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
                ui.button("Save to programs folder", on_click=save).props(
                    "flat color=wc-text"
                ).mark("installation-toml-save")
                ui.button("Close", on_click=dismiss).props(
                    "unelevated color=wc-control text-color=wc-text"
                )
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
                f"{kind.capitalize()} here",
                on_click=lambda k=kind, p=click_point: self._place_shape(k, p),
            ).mark(f"scene-add-{kind}")

    async def _place_shape(self, kind: str, at: tuple[float, float, float]) -> None:
        """A default-sized *kind* resting on the floor at *at*, selected."""
        handle = self._shape_handle()
        if handle is None or kind not in _PLACEABLE:
            return
        dims = dict(_DEFAULT_DIMS[kind])
        lift = (
            dims["z"] / 2
            if kind == "box"
            else dims["length"] / 2
            if kind == "cylinder"
            else dims["radius"]
        )
        # To the millimetre, as a position typed into the program would be.
        x, y, z = (round(float(v), 3) for v in at)
        shape = _KINDS[kind](
            name=self._fresh_shape_name(kind),
            pose=(x, y, z + lift, 0.0, 0.0, 0.0),
            **dims,
        )
        if await self._commit_objects(
            "Create object", [*handle.shapes, shape], self.object_history.context()
        ):
            self._select_objects([f"{SHAPE_PREFIX}{shape.name}"])

    def _open_attachment_form(self, name: str, *, detach: bool = False) -> None:
        """Select *name* with its attach, reconcile or detach form open in
        the inspector beside it."""
        key = f"{SHAPE_PREFIX}{name}"
        if self._program_shape(name) is None or self._shape_locked(name):
            return
        self._select_objects([key])
        self._attachment_form = (key, detach)
        self._attachment_revision = self.object_history.context()
        self._refresh_object_inspector()

    def _close_attachment_form(self) -> None:
        self._attachment_form = None
        self._refresh_object_inspector()

    def _attachment_section(self, entity: str, shape: Shape) -> None:
        """The open attachment form: the declared pose, and for an attach
        the collision partners the held object may touch."""
        detach = self._attachment_form == (entity, True)
        initial = shape.pose if shape.attachment is not None and not detach else None
        title = (
            "Detach to world"
            if detach
            else "Reconcile attachment"
            if shape.attachment is not None
            else "Attach to flange"
        )
        with (
            ui.expansion(title, value=True)
            .props("dense header-class=text-xs")
            .classes("w-full")
            .mark("object-attachment")
        ):
            ui.label(
                "Resting pose in the world frame · mm, °"
                if detach
                else "Pose relative to the flange · mm, °"
            ).classes("text-xs text-wc-text-muted")
            with ui.row().classes("w-full no-wrap gap-1"):
                pos = [
                    ui.number(
                        axis,
                        value=None if initial is None else round(initial[i] * 1000, 3),
                    )
                    .props("dense borderless hide-bottom-space")
                    .classes("min-w-0 flex-1")
                    .mark(f"attachment-pos-{axis.lower()}")
                    for i, axis in enumerate(("X", "Y", "Z"))
                ]
            with ui.row().classes("w-full no-wrap gap-1"):
                rot = [
                    ui.number(
                        axis,
                        value=0
                        if initial is None
                        else round(math.degrees(initial[i + 3]), 3),
                    )
                    .props("dense borderless hide-bottom-space")
                    .classes("min-w-0 flex-1")
                    .mark(f"attachment-rot-{axis.lower()}")
                    for i, axis in enumerate(("Rx", "Ry", "Rz"))
                ]
            contacts = None
            if not detach:
                contacts = (
                    ui.textarea(
                        "Allowed contacts",
                        value="\n".join(shape.attachment.allowed_contacts)
                        if shape.attachment
                        else "",
                    )
                    .props(
                        'dense outlined rows=2 hint="One collision-report name per line, e.g. shape:fixture or L6"'
                    )
                    .classes("w-full")
                    .mark("attachment-contacts")
                )
            ui.label(
                "Declares the model only; it does not operate the gripper."
            ).classes("text-xs text-wc-text-muted")
            feedback = (
                ui.label()
                .classes("text-xs text-wc-warning")
                .mark("attachment-feedback")
            )
            with ui.row().classes("w-full justify-end no-wrap").style("gap:4px"):
                ui.button("Cancel", on_click=self._close_attachment_form).props(
                    "flat dense no-caps color=wc-text"
                )
                button = (
                    ui.button("Declare")
                    .props(
                        "unelevated dense no-caps color=wc-action text-color=wc-on-bright"
                    )
                    .mark("attachment-apply")
                )
                button.on_click(
                    lambda: self._apply_attachment(
                        shape.name, detach, pos, rot, contacts, feedback, button
                    )
                )

    async def _apply_attachment(
        self,
        name: str,
        detach: bool,
        pos: list[Any],
        rot: list[Any],
        contacts: Any,
        feedback: Any,
        button: Any,
    ) -> None:
        from waldo_commander.skills import attach_object, detach_object

        shape = self._program_shape(name)
        if shape is None:
            return
        button.disable()
        started_at = time.time()
        try:
            # Declaring is how a refused attachment is reconciled.
            self.object_history.guard(self._attachment_revision, confirmed=False)
            if self._shape_locked(name):
                raise ValueError(f"Unlock {name} before editing it")
            blank = [
                field._props.get("label", "")
                for field in (*pos, *rot)
                if field.value is None
            ]
            if blank:
                # float(None) would reach the feedback label as a raw Python
                # type error, which tells the operator nothing about which box
                # to fill in.
                raise ValueError(
                    f"Fill in {', '.join(blank)} before declaring this attachment"
                )
            pose = cast(
                Pose6,
                tuple(
                    [float(v.value) / 1000 for v in pos]
                    + [math.radians(float(v.value)) for v in rot]
                ),
            )
            client = waldoctl.commander.client
            if not require_browser_control(ui_state.active_client_id):
                return
            if detach:
                result = await detach_object.async_call(
                    client, name=name, world_pose=pose, shape=shape
                )
                source = f"from waldo_commander.skills import detach_object\ndetach_object(rbt, name={name!r}, world_pose={pose!r})"
            else:
                names = (
                    tuple(
                        n.strip() for n in str(contacts.value).splitlines() if n.strip()
                    )
                    if contacts
                    else ()
                )
                result = await attach_object.async_call(
                    client,
                    name=name,
                    flange_pose=pose,
                    allowed_contacts=names,
                    shape=shape,
                )
                source = f"from waldo_commander.skills import attach_object\nattach_object(rbt, name={name!r}, flange_pose={pose!r}, allowed_contacts={names!r})"
            handle = self._shape_handle()
            if handle is not None:
                await handle.refresh_from_backend()
            from waldo_commander.services.motion_recorder import motion_recorder

            motion_recorder.record_completed_skill(source, started_at=started_at)
            self._notify(
                f"{'Detached' if result.attachment is None else 'Attachment confirmed'}: {name}"
            )
            self._close_attachment_form()
        except Exception as error:
            logger.info("Attachment declaration refused: %s", error)
            if not feedback.is_deleted:
                feedback.set_text(str(error))
        finally:
            if not button.is_deleted:
                button.enable()

    async def _delete_shape(self, name: str) -> None:
        """Delete *name* at once; the object history's Undo brings it back."""
        handle = self._shape_handle()
        if handle is None or self._shape_locked(name):
            return
        if self._shape_move_active == name:
            self._end_shape_move()
        await self._commit_objects(
            "Delete object",
            [s for s in handle.shapes if s.name != name],
            self.object_history.context(),
        )

    # ------------------------------------------------------------------
    # Drag-to-move
    # ------------------------------------------------------------------

    def _movable(self, key: str) -> bool:
        """Whether the program-layer object *key* may be dragged."""
        if (
            not key.startswith(SHAPE_PREFIX)
            or key in self._locked_objects
            or key in self._hidden_objects
        ):
            return False
        shape = self._program_shape(key[len(SHAPE_PREFIX) :])
        return shape is not None and shape.attachment is None

    def _start_shape_move(self, name: str) -> None:
        shape = self._program_shape(name)
        if shape is None or shape.attachment is not None or self._shape_locked(name):
            return
        if f"{SHAPE_PREFIX}{name}" not in self._shape_objects:
            return
        self._cancel_shape_drawing()
        self._shape_move_active = name
        selected = tuple(k[len(SHAPE_PREFIX) :] for k in self._selected_objects)
        self._shape_move_members = (
            selected
            if len(selected) > 1
            and name in selected
            and all(self._movable(k) for k in self._selected_objects)
            else (name,)
        )
        self._shape_move_document = self.object_history.document()
        self._shape_move_session += 1
        self._push_interaction()

    def _end_shape_move(self) -> None:
        if self._shape_move_active is None:
            return
        self._shape_move_active = None
        self._shape_move_members = ()
        self._shape_move_document = None
        self._shape_move_session += 1
        self._push_interaction()

    def _shape_move_state(self) -> dict[str, Any] | None:
        """The keep-out the browser shows move arrows on, and its session."""
        name = self._shape_move_active
        obj = None if name is None else self._shape_objects.get(f"{SHAPE_PREFIX}{name}")
        if obj is None:
            return None
        shape = self._program_shape(name)
        if shape is None:
            return None
        from scipy.spatial.transform import Rotation

        state = {
            "primitive": shape.kind,
            "dimensions": {
                key: getattr(shape, key) for key in param_names(type(shape))
            },
            "quaternion": Rotation.from_matrix(pose_matrix(shape.pose)[:3, :3])
            .as_quat()
            .tolist(),
            "session": self._shape_move_session,
            "node": obj.id,
            "mode": self._object_mode,
            "symmetric": self._resize_centre,
            "axes": RESIZE_AXES.get(shape.kind),
            "revision": self._shape_handle().revision,
        }
        if len(self._shape_move_members) > 1:
            shapes, nodes = [], []
            for name in self._shape_move_members:
                member = self._program_shape(name)
                node = self._shape_objects.get(f"{SHAPE_PREFIX}{name}")
                if member is None or node is None:
                    return None
                shapes.append(member)
                nodes.append(node)
            state.update(
                members=[node.id for node in nodes],
                center=[sum(s.pose[i] for s in shapes) / len(shapes) for i in range(3)],
            )
        return state

    def _shape_admit(self, gesture: Any, args: dict[str, Any]) -> bool:
        """Admit a drag on the moving keep-out as a group move, a resize or a
        single move, whichever the selection and object mode make it."""
        name = self._shape_move_active
        if (
            name is None
            or args.get("session") != self._shape_move_session
            or args.get("revision") != self._shape_handle().revision
        ):
            return False
        obj = self._shape_objects.get(f"{SHAPE_PREFIX}{name}")
        shape = self._program_shape(name)
        if (
            obj is None
            or obj.id != args.get("node")
            or shape is None
            or shape.attachment is not None
            or self._shape_locked(name)
        ):
            return False
        gesture.data.update(
            name=name,
            node=obj,
            shape=shape,
            revision=self._shape_handle().revision,
            context=self.object_history.context(),
        )
        resizing = self._object_mode == "resize"
        if len(self._shape_move_members) > 1:
            admitted = not resizing and self._group_admit(gesture, args)
        elif resizing:
            admitted = self._resize_admit(gesture, args)
        else:
            admitted = self._move_admit(gesture, args)
        return admitted

    def _group_admit(self, gesture: Any, args: dict[str, Any]) -> bool:
        keys = [f"{SHAPE_PREFIX}{n}" for n in self._shape_move_members]
        nodes = [self._shape_objects.get(k) for k in keys]
        mode = args.get("mode", "translate")
        if (
            mode not in ("translate", "rotate")
            or not all(self._movable(k) for k in keys)
            or any(n is None for n in nodes)
        ):
            return False
        members = tuple(self._program_shape(n) for n in self._shape_move_members)
        gesture.data.update(
            members=members, nodes=nodes, mode=mode, end=self._finish_selection_move
        )
        return True

    def _resize_admit(self, gesture: Any, args: dict[str, Any]) -> bool:
        if (
            gesture.data["shape"].kind not in RESIZE_AXES
            or args.get("mode", "resize") != "resize"
            or type(args.get("axis")) is not int
            or args["axis"] not in (0, 1, 2)
            or args.get("sign") not in (-1, 1)
            or type(args.get("symmetric")) is not bool
            or (self._resize_centre and not args["symmetric"])
        ):
            return False
        gesture.data.update(
            axis=args["axis"],
            sign=args["sign"],
            symmetric=args["symmetric"],
            mode="resize",
            end=self._finish_resize,
        )
        return True

    def _move_admit(self, gesture: Any, args: dict[str, Any]) -> bool:
        mode = args.get("mode", "translate")
        if mode not in ("translate", "rotate"):
            return False
        gesture.data.update(mode=mode, end=self._finish_move)
        return True

    def _shape_finish(
        self, gesture: Any, args: dict[str, Any] | None, aborted: bool
    ) -> None:
        gesture.data["end"](gesture, args, aborted)

    def _shape_landed(
        self, gesture: Any, args: dict[str, Any] | None, aborted: bool, *keys: str
    ) -> list[float] | None:
        """The released values, if the move and the keep-out are still the
        ones the drag began on; otherwise the app's position stands."""
        name, obj, shape = (gesture.data[k] for k in ("name", "node", "shape"))
        values = None if aborted or args is None else finite(args, *keys)
        landed = (
            values is not None
            and args is not None
            and args.get("session") == self._shape_move_session
            and self._shape_move_active == name
            # The same keep-out, though a readback may have rebuilt it.
            and self._program_shape(name) == shape
            and self._shape_handle().revision == gesture.data["revision"]
            and self.object_history.context() == gesture.data["context"]
            and self._shape_objects.get(f"{SHAPE_PREFIX}{name}") is obj
        )
        if not landed:
            obj.resend_pose()
            return None
        return values

    def _commit_shape(self, gesture: Any, label: str, moved: Shape) -> None:
        """Write *moved* through the request path; the re-render is a diff,
        so the dragged object, and the arrows on it, survive for the next
        nudge."""
        obj = gesture.data["node"]
        shapes = [
            moved if s.name == moved.name else s for s in self._shape_handle().shapes
        ]

        async def commit():
            try:
                await self._commit_objects(label, shapes, gesture.data["context"])
            finally:
                if not obj.deleted:
                    obj.resend_pose()

        self._spawn(commit())

    def _finish_resize(
        self, gesture: Any, args: dict[str, Any] | None, aborted: bool
    ) -> None:
        values = self._shape_landed(gesture, args, aborted, "size")
        if values is None:
            return
        shape, size = gesture.data["shape"], values[0]
        axis, sign = gesture.data["axis"], gesture.data["sign"]
        dimension = RESIZE_AXES[shape.kind][axis]
        if size <= 0 or abs(size - getattr(shape, dimension)) <= _UNMOVED:
            gesture.data["node"].resend_pose()
            return
        center = list(shape.pose[:3])
        if dimension != "radius" and not gesture.data["symmetric"]:
            shift = (size - getattr(shape, dimension)) * sign / 2
            direction = pose_matrix(shape.pose)[:3, axis]
            center = [value + shift * direction[i] for i, value in enumerate(center)]
        self._commit_shape(
            gesture,
            "Resize object",
            replace(shape, **{dimension: size}, pose=(*center, *shape.pose[3:])),
        )

    def _finish_move(
        self, gesture: Any, args: dict[str, Any] | None, aborted: bool
    ) -> None:
        values = self._shape_landed(gesture, args, aborted, "x", "y", "z")
        if values is None:
            return
        shape = gesture.data["shape"]
        position = [
            old if abs(new - old) <= _UNMOVED else new
            for new, old in zip(values, shape.pose[:3])
        ]
        rotation = shape.pose[3:]
        if gesture.data["mode"] == "rotate":
            import numpy as np
            from scipy.spatial.transform import Rotation
            from .urdf_scene import _Y_UP_KINDS, _Y_TO_Z_UP

            q = finite(args, "qx", "qy", "qz", "qw")
            if q is None or np.linalg.norm(q) < 1e-6:
                gesture.data["node"].resend_pose()
                return
            matrix = Rotation.from_quat(q).as_matrix()
            if shape.kind in _Y_UP_KINDS:
                matrix = matrix @ _Y_TO_Z_UP.T
            if not np.allclose(
                matrix, pose_matrix(shape.pose)[:3, :3], rtol=0, atol=_UNMOVED
            ):
                rotation = tuple(Rotation.from_matrix(matrix).as_euler("xyz"))
        moved = replace(shape, pose=(*position, *rotation))
        if moved == shape:
            gesture.data["node"].resend_pose()
            return
        self._commit_shape(
            gesture,
            "Rotate object" if gesture.data["mode"] == "rotate" else "Move object",
            moved,
        )

    def _finish_selection_move(self, gesture, args, aborted):
        import numpy as np
        from scipy.spatial.transform import Rotation

        members, nodes = gesture.data["members"], gesture.data["nodes"]

        def restore():
            for node in nodes:
                if not node.deleted:
                    node.resend_pose()
            # A released frame stays where it was let go until a new move
            # session seats it again; an aborted one goes back by itself.
            if not aborted and self._shape_move_active is not None:
                self._shape_move_session += 1
                self._push_interaction()

        position = None if aborted or args is None else finite(args, "x", "y", "z")
        if (
            position is None
            or args.get("session") != self._shape_move_session
            or tuple(s.name for s in members) != self._shape_move_members
            or tuple(self._program_shape(s.name) for s in members) != members
            or self.object_history.context() != gesture.data["context"]
        ):
            restore()
            return
        rotation = Rotation.identity()
        if gesture.data["mode"] == "rotate":
            q = finite(args, "qx", "qy", "qz", "qw")
            if q is None or np.linalg.norm(q) < 1e-6:
                restore()
                return
            rotation = Rotation.from_quat(q)
        center = np.mean([s.pose[:3] for s in members], axis=0)
        if (
            np.allclose(position, center, rtol=0, atol=_UNMOVED)
            and rotation.magnitude() <= _UNMOVED
        ):
            restore()
            return
        moved = {
            s.name: replace(
                s,
                pose=(
                    *(
                        np.asarray(position)
                        + rotation.apply(np.asarray(s.pose[:3]) - center)
                    ),
                    *(
                        rotation * Rotation.from_matrix(pose_matrix(s.pose)[:3, :3])
                    ).as_euler("xyz"),
                ),
            )
            for s in members
        }

        async def commit():
            try:
                await self._commit_objects(
                    "Rotate selection"
                    if gesture.data["mode"] == "rotate"
                    else "Move selection",
                    [moved.get(s.name, s) for s in self._shape_handle().shapes],
                    gesture.data["context"],
                )
            finally:
                restore()

        self._spawn(commit())
