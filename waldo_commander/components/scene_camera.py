"""The camera's buttons under the scene's view cube."""

from typing import Any

from nicegui import ui

from waldo_commander.scene3d.element import WcScene

_PROJECTION_ICONS = "svguse:/static/icons/projection.svg#{}"


def camera_action(action: str) -> None:
    for element in ui.context.client.elements.values():
        if isinstance(element, WcScene):
            element.run_method("camera_action", action)
            break


def build_camera_menu(scene: WcScene) -> None:
    """Reset, focus and the two projections, under *scene*'s view cube."""

    def act(action: str) -> None:
        scene.run_method("camera_action", action)

    host = scene.parent_slot
    assert host is not None  # a scene on the page always has one

    flat = "flat round dense color=wc-text"
    with host, ui.element("div").classes("wc-viewcube-tools glass"):
        ui.button(icon="restart_alt", on_click=lambda: act("reset")).props(
            flat
        ).tooltip("Reset view").mark("camera-reset")
        ui.button(icon="center_focus_strong", on_click=lambda: act("focus")).props(
            flat
        ).tooltip("Focus · F").mark("camera-focus")
        ui.separator().props("vertical")
        projections = {
            name: ui.button(
                icon=_PROJECTION_ICONS.format(name),
                on_click=lambda _, n=name: act(n),
            )
            .props(flat)
            .tooltip(name.capitalize())
            .mark(f"camera-{name}")
            for name in ("perspective", "orthographic")
        }

    def changed(status: dict[str, Any]) -> None:
        shown = "orthographic" if status["orthographic"] else "perspective"
        for name, button in projections.items():
            on = name == shown
            button.props(
                f"flat round dense color={'wc-action' if on else 'wc-text'} "
                f"aria-pressed={'true' if on else 'false'}"
            )

    scene.on_camera(changed)
    act("status")
