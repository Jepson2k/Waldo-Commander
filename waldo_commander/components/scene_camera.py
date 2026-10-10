"""The camera's buttons beside the scene's view cube: home and the views menu."""

from typing import Any

from nicegui import ui

from waldo_commander.scene3d.element import WcScene


def camera_action(action: str) -> None:
    for element in ui.context.client.elements.values():
        if isinstance(element, WcScene):
            element.run_method("camera_action", action)
            break


def build_camera_menu(scene: WcScene) -> None:
    """The home button and the camera menu, beside *scene*'s view cube."""

    def act(action: str) -> None:
        scene.run_method("camera_action", action)

    state: dict[str, Any] = {"orthographic": False, "back": False}
    host = scene.parent_slot
    assert host is not None  # a scene on the page always has one

    with host, ui.element("div").classes("wc-viewcube-tools glass"):
        ui.button(icon="home", on_click=lambda: act("reset")).props(
            "flat round dense color=wc-text"
        ).tooltip("Reset camera").mark("camera-home")
        with (
            ui.button(icon="expand_more")
            .props("flat round dense color=wc-text")
            .tooltip("Camera views · F to focus")
            .mark("camera-menu") as button,
            ui.menu()
            .classes("bg-wc-surface text-wc-text min-w-[280px]")
            # Down and to the left, clear of the cube on its right.
            .props('anchor="bottom right" self="top right" :offset="[0, 4]"')
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
            ui.separator()
            isolate = (
                ui.menu_item("Isolate selection", on_click=lambda: act("isolate"))
                .props("dense")
                .mark("camera-isolate")
            )
            isolate.disable()
            isolate.set_visibility(False)
            restore_objects = (
                ui.menu_item(
                    "Show all objects", on_click=lambda: act("restore_objects")
                )
                .props("dense")
                .mark("camera-restore-objects")
            )
            restore_objects.set_visibility(False)
            ui.menu_item("Reset camera", on_click=lambda: act("reset")).props(
                "dense"
            ).mark("reset-camera")

    def changed(status: dict[str, Any]) -> None:
        state.update(status)
        projection.set_value("orthographic" if state["orthographic"] else "perspective")
        back.set_enabled(state["back"])
        isolated = bool(state.get("isolated", False))
        can_isolate = bool(state.get("canIsolate", False))
        isolate.set_enabled(can_isolate)
        isolate.set_visibility(not isolated and can_isolate)
        restore_objects.set_visibility(isolated)
        button.props(
            "flat round dense color=wc-action"
            if isolated
            else "flat round dense color=wc-text"
        )

    scene.on_camera(changed)
    act("status")
