"""A new keep-out in the scene panel, at a right-clicked point or face:
placed there at a default size, or drawn there with the pointer.

While its drawing runs the card holds the drawing's height field, Create and
Cancel; the view shows only the size, by the pointer.
"""

from __future__ import annotations

import math

from nicegui import ui

from waldo_commander.scene3d.interaction import finite

_SHAPES = {"box": "Box", "sphere": "Sphere", "cylinder": "Cylinder"}


class NewKeepout:
    """The card, at the top of the scene panel over the list of objects."""

    def __init__(self, view, host, surface, *, installation: bool) -> None:
        self.view, self.surface, self.installation = view, surface, installation
        # The drawing this card last started, and its phase as the view says.
        self.session: int | None = None
        self.phase = "armed"
        self.step = 0.0
        self.busy = False
        self.closed = False
        # A face is offered as it was when right-clicked.
        key = surface.get("id")
        self.face = None if key is None else (key, view._object_shape(key))
        with host:
            self.card = ui.column().classes("scene-library w-full").mark("keepout-card")
            self.card.on("keydown.escape", self.close)
            with self.card:
                self._build()
        self.follow()

    def _build(self) -> None:
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label(
                "New installation shape" if self.installation else "New keep-out"
            ).classes("scene-section flex-1")
            ui.button(icon="close", on_click=self.close).props(
                "flat round dense color=wc-text"
            ).tooltip("Close · Esc").mark("keepout-close")
        ui.label(self._where()).classes("text-xs text-wc-text-muted").mark(
            "keepout-surface"
        )
        self.shape = (
            ui.toggle(_SHAPES, value="box")
            .props(
                "dense no-caps unelevated spread"
                " toggle-color=wc-action toggle-text-color=wc-on-bright"
            )
            .classes("w-full")
            .mark("keepout-shape")
        )
        with ui.row().classes("w-full items-center no-wrap gap-1") as self.start:
            self.place = (
                ui.button("Place here", icon="place", on_click=self._place)
                .props(
                    "unelevated dense no-caps color=wc-action text-color=wc-on-bright"
                )
                .tooltip("At a default size, standing on the point")
                .mark("keepout-place")
            )
            self.draw = (
                ui.button("Draw", icon="draw", on_click=self._draw)
                .props("flat dense no-caps color=wc-text")
                .tooltip("Drag out its size in the view")
                .mark("keepout-draw")
            )
        with ui.column().classes("w-full gap-1") as self.drawing:
            self.hint = (
                ui.label().classes("text-xs text-wc-text-muted").mark("keepout-hint")
            )
            with ui.row().classes("w-full items-center no-wrap gap-1"):
                self.height = (
                    ui.number(
                        prefix="Height", suffix="mm", on_change=self._height_changed
                    )
                    .props("dense borderless hide-bottom-space")
                    .classes("scene-field min-w-0 flex-1")
                    .mark("keepout-height")
                )
                self.height.on("keydown.enter", self._create)
                self.create = (
                    ui.button("Create", on_click=self._create)
                    .props(
                        "unelevated dense no-caps color=wc-action text-color=wc-on-bright"
                    )
                    .mark("keepout-create")
                )
                ui.button(
                    "Cancel", on_click=lambda: self.view._cancel_shape_drawing()
                ).props("flat dense no-caps color=wc-text").mark("keepout-cancel")
        self.feedback = (
            ui.label().classes("text-xs text-wc-error").mark("keepout-feedback")
        )

    def _where(self) -> str:
        label = self.surface["label"]
        if self.face is not None:
            return label
        x, y = (round(v * 1000) for v in self.surface["origin"][:2])
        return f"{label} at {x}, {y} mm"

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        view = self.view
        if view._keepout_card is self:
            view._keepout_card = None
        drawing = view._drawing
        if drawing is not None and drawing["session"] == self.session:
            view._cancel_shape_drawing()
        if not self.card.is_deleted:
            self.card.delete()

    def follow(self) -> None:
        """Show the drawing's controls while this card's drawing runs."""
        if self.closed or self.card.is_deleted:
            return
        drawing = self.view._drawing
        active = drawing is not None and drawing["session"] == self.session
        if not active:
            self.phase, self.step = "armed", 0.0
        self.start.set_visibility(not active)
        self.drawing.set_visibility(active)
        self.shape.set_enabled(not active and not self.busy)
        self.place.set_enabled(not self.busy)
        self.draw.set_enabled(not self.busy)
        if not active:
            return
        kind = drawing["primitive"]
        tall = self.phase == "height"
        if tall:
            hint = "Pull up the top handle, or enter the height"
        elif kind == "box":
            hint = "Drag its footprint in the view"
        else:
            hint = "Drag from its centre to set the radius"
        if self.step:
            hint += f" · {self.step * 1000:g} mm steps"
        self.hint.set_text(f"{hint} · Esc cancels")
        for element in (self.height, self.create):
            element.set_visibility(kind != "sphere")
            element.set_enabled(tall)

    def show_phase(self, args) -> None:
        """The view's drawing began a phase: its footprint, then its height."""
        phase = args.get("phase")
        if (
            self.closed
            or args.get("session") != self.session
            or phase not in ("armed", "footprint", "height")
        ):
            return
        self.phase = phase
        step = finite(args, "step")
        self.step = step[0] if step else 0.0
        height = finite(args, "height")
        if phase == "height" and height is not None:
            self.height.set_value(round(height[0] * 1000, 1))
        self.follow()

    def _ready(self) -> bool:
        """Whether the face is still as it was right-clicked."""
        face = self.face
        if face is not None and self.view._object_shape(face[0]) != face[1]:
            self.feedback.set_text(
                f"{self.surface['label']} has changed. Right-click it again."
            )
            return False
        self.feedback.set_text("")
        return True

    async def _place(self) -> None:
        if self.busy or not self._ready():
            return
        self.busy = True
        self.follow()
        try:
            placed = await self.view._place_shape(
                self.shape.value, self.surface, installation=self.installation
            )
        finally:
            self.busy = False
        if placed:
            self.close()
        elif not self.closed:
            self.feedback.set_text(self.view.object_history.feedback)
            self.follow()

    def _draw(self) -> None:
        if self.busy or not self._ready():
            return
        view = self.view
        try:
            self.session = view._start_shape_drawing(
                self.shape.value,
                self.surface,
                view.object_history.context(),
                installation=self.installation,
            )
        except ValueError as error:
            self.feedback.set_text(str(error))
            return
        self.follow()

    def _entered(self) -> float | None:
        """The height typed, in metres, if it is one."""
        value = self.height.value
        if value is None or not math.isfinite(value) or value <= 0:
            return None
        return value / 1000

    def _height_changed(self) -> None:
        height = self._entered()
        if self.phase == "height" and height is not None:
            self.view.scene.run_method("drawing", self.session, "height", height)

    def _create(self) -> None:
        if self.phase != "height":
            return
        height = self._entered()
        if height is None:
            self.feedback.set_text("Enter a height above zero")
            return
        self.feedback.set_text("")
        self.view.scene.run_method("drawing", self.session, "create", height)
