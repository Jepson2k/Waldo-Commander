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


def _index(textarea, line: int, column: int = 1) -> int:
    """The ``str`` index of 1-based *line* and *column* in the editor's text,
    which is how CodeMirror reports a selection."""
    rows = str(textarea.value or "").split("\n")
    return sum(len(row) + 1 for row in rows[: line - 1]) + column - 1


def _selection_change(textarea, anchor: int, head: int) -> dict:
    """The selection-change payload of one range from *anchor* to *head*."""
    return {"ranges": [{"anchor": anchor, "head": head}], "main_index": 0}


def _place_cursor(textarea, line: int, column: int = 1) -> None:
    """A selection change that puts the cursor at *line* and *column*."""
    index = _index(textarea, line, column)
    _fire_editor_event(
        textarea, "selection-change", _selection_change(textarea, index, index)
    )


def _set_cursor_line(textarea, line: int) -> None:
    """Place the cursor like a user click: focus, then a selection change.
    Focus first — the editor only trusts selection-changes on a focused tab
    (unfocused ones are echoes of programmatic value updates)."""
    _fire_editor_event(textarea, "focus-change", {"focused": True})
    _place_cursor(textarea, line)


def _set_selection(textarea, from_line: int, to_line: int) -> None:
    """Select a line range like a user drag, from the start of *from_line* to
    the end of *to_line*."""
    rows = str(textarea.value or "").split("\n")
    _fire_editor_event(textarea, "focus-change", {"focused": True})
    _fire_editor_event(
        textarea,
        "selection-change",
        _selection_change(
            textarea,
            _index(textarea, from_line),
            _index(textarea, to_line, len(rows[to_line - 1]) + 1),
        ),
    )
