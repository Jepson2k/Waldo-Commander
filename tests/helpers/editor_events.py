"""Drive CodeMirror editor events through the element's real listeners."""

from __future__ import annotations


def _fire_editor_event(textarea, event_type: str, args: dict) -> None:
    """Drive a CodeMirror event through the element's real event listener —
    the same path a browser event takes. ``Element.on`` stores listener types
    camelCased, so kebab-case names are converted before matching."""
    from nicegui.helpers import event_type_to_camel_case

    wanted = event_type_to_camel_case(event_type)
    listener = next(
        (
            listener
            for listener in textarea._event_listeners.values()
            if listener.type == wanted
        ),
        None,
    )
    assert listener is not None, f"no {event_type} listener registered"
    with textarea.client:
        textarea._handle_event({"listener_id": listener.id, "args": args})


def _set_cursor_line(textarea, line: int) -> None:
    """Place the cursor like a user click: focus, then a selection change.
    Focus first — the editor only trusts selection-changes on a focused tab
    (unfocused ones are echoes of programmatic value updates)."""
    _fire_editor_event(textarea, "focus-change", {"focused": True})
    _fire_editor_event(
        textarea,
        "selection-change",
        {"line": line, "column": 1, "from_line": line, "to_line": line, "empty": True},
    )


def _set_selection(textarea, from_line: int, to_line: int) -> None:
    """Select a line range like a user drag (head at the selection end)."""
    _fire_editor_event(textarea, "focus-change", {"focused": True})
    _fire_editor_event(
        textarea,
        "selection-change",
        {
            "line": to_line,
            "column": 1,
            "from_line": from_line,
            "to_line": to_line,
            "empty": False,
        },
    )
