"""Camera navigation beside the scene's existing movement controls."""

from nicegui import ui

from waldo_commander.scene3d.element import WcScene


def camera_action(action: str) -> None:
    for element in ui.context.client.elements.values():
        if isinstance(element, WcScene):
            element.run_method("camera_action", action)
            break


def build_camera_menu() -> None:
    client = ui.context.client
    scene: WcScene | None = None

    def act(action: str, value=None) -> None:
        if scene is not None:
            scene.run_method("camera_action", action, value)

    state = {"orthographic": False, "back": False}

    with (
        ui.button(icon="view_in_ar")
        .props("round unelevated dense color=wc-control text-color=wc-text")
        .tooltip("Camera views · F to focus")
        .mark("camera-menu") as button,
        ui.menu()
        .classes("bg-wc-surface text-wc-text")
        .props('anchor="top right" self="bottom right"')
        .classes("min-w-[280px]")
        .mark("camera-views") as menu,
    ):
        ui.element("q-resize-observer").on(
            "resize", js_handler=f"() => getElement({menu.id})?.updatePosition()"
        )
        ui.menu_item(
            "Focus inspection / robot",
            on_click=lambda: act("focus"),
        ).props("dense").mark("camera-focus").tooltip(
            "Frames the inspected pose, object being edited, or robot · F"
        )
        back = (
            ui.menu_item(
                "Back to previous view",
                on_click=lambda: act("previous"),
            )
            .props("dense")
            .mark("camera-back")
            .tooltip("Restore camera position, projection and zoom · Shift+F")
        )
        back.disable()
        ui.separator()
        with ui.row().classes("px-4 py-2"):
            projection = (
                ui.toggle(
                    {"perspective": "Perspective", "orthographic": "Orthographic"},
                    value="perspective",
                    on_change=lambda e: act(e.value)
                    if (e.value == "orthographic") != state["orthographic"]
                    else None,
                )
                .props(
                    "no-caps dense color=wc-control text-color=wc-text toggle-color=wc-action toggle-text-color=wc-on-bright"
                )
                .mark("camera-projection")
            )
        with ui.row().classes("px-4 pb-2"):
            for action, label in (
                ("front", "Front"),
                ("right", "Right"),
                ("top", "Top"),
            ):
                ui.button(
                    label,
                    on_click=lambda _, a=action: act(a),
                ).props("flat dense no-caps color=wc-text").mark(f"camera-{action}")
        with (
            ui.expansion("Section objects", icon="content_cut")
            .props("dense")
            .classes("w-full")
            .mark("camera-section-options")
        ):
            ui.element("q-resize-observer").on(
                "resize", js_handler=f"() => getElement({menu.id})?.updatePosition()"
            )
            enabled = (
                ui.switch(
                    "Enable section",
                    value=False,
                    on_change=lambda e: act("section", e.value)
                    if e.value != state.get("section", False)
                    else None,
                )
                .props("dense")
                .classes("mx-4 my-2")
                .mark("camera-section-enabled")
            )
            with ui.column().classes("w-full px-4 pb-2 gap-2") as section_controls:
                axis = (
                    ui.toggle(
                        {"view": "View", "x": "X", "y": "Y", "z": "Z"},
                        value="view",
                        on_change=lambda e: act("section_axis", e.value)
                        if e.value != state.get("sectionAxis", "view")
                        else None,
                    )
                    .props(
                        "no-caps dense color=wc-control text-color=wc-text toggle-color=wc-action toggle-text-color=wc-on-bright"
                    )
                    .mark("camera-section-axis")
                )
                offset = (
                    ui.number("Offset from center · mm", value=0, format="%.1f")
                    .props("dense outlined")
                    .classes("w-full")
                    .mark("camera-section-offset")
                )
                offset.on("blur", lambda: act("status"))
                offset.on("keydown.enter", lambda: act("status"))
                slider = (
                    ui.slider(min=-1000, max=1000, step=1, value=0)
                    .props(
                        "color=wc-action text-color=wc-on-bright label label-color=wc-action label-text-color=wc-on-bright"
                    )
                    .classes("w-full")
                    .bind_value(offset)
                    .mark("camera-section-slider")
                )
                for field in (offset, slider):
                    field.on(
                        "update:model-value",
                        js_handler="""(value) => {
                      if(value === null || value === '' || !Number.isFinite(Number(value)))return;
                      const root=document.querySelector('.wc-scene');
                      if(root)getElement(root)?.camera_action('section_offset',Number(value));
                    }""",
                    )
                ui.button(
                    "Flip visible side",
                    icon="flip",
                    on_click=lambda: act("section_flip"),
                ).props("flat dense no-caps color=wc-text").mark("camera-section-flip")
                ui.label("Objects only · robot and paths stay visible").classes(
                    "text-xs text-wc-text-muted"
                )
            section_controls.set_visibility(False)
            section_message = (
                ui.label()
                .classes("text-xs text-wc-text-muted")
                .mark("camera-section-message")
            )
            section_message.set_visibility(False)
        ui.separator()
        isolate = (
            ui.menu_item("Isolate selected objects", on_click=lambda: act("isolate"))
            .props("dense")
            .mark("camera-isolate")
        )
        isolate.disable()
        isolate.set_visibility(False)
        restore_objects = (
            ui.menu_item(
                "Restore surrounding objects", on_click=lambda: act("restore_objects")
            )
            .props("dense")
            .mark("camera-restore-objects")
        )
        restore_objects.set_visibility(False)
        ui.menu_item("Reset camera", on_click=lambda: act("reset")).props("dense").mark(
            "reset-camera"
        )

    def changed(status):
        state.update(status)
        projection.set_value("orthographic" if state["orthographic"] else "perspective")
        back.set_enabled(state["back"])
        isolated = bool(state.get("isolated", False))
        isolate.set_enabled(bool(state.get("canIsolate", False)))
        isolate.set_visibility(not isolated and bool(state.get("canIsolate", False)))
        restore_objects.set_visibility(isolated)
        active = bool(state.get("section", False))
        enabled.set_value(active)
        section_controls.set_visibility(active)
        axis.set_value(state.get("sectionAxis", "view"))
        offset.set_value(state.get("sectionOffset", 0))
        extent = state.get("sectionRange", 1000)
        slider.props["min"] = -extent
        slider.props["max"] = extent
        section_message.set_text(state.get("sectionMessage", ""))
        section_message.set_visibility(bool(section_message.text))
        button.props(
            "color=wc-action text-color=wc-on-bright"
            if active or isolated
            else "color=wc-control text-color=wc-text"
        )

    button.disable()

    def bind_scene() -> None:
        nonlocal scene
        scene = next(
            (e for e in client.elements.values() if isinstance(e, WcScene)), None
        )
        if scene is not None:
            scene.on_camera(changed)
            act("status")
            button.enable()
            timer.cancel()

    timer = ui.timer(0.1, bind_scene)
