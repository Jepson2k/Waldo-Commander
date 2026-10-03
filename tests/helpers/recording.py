"""Reading what a recording take has written into the editor."""

from __future__ import annotations


def staged_lines(textarea) -> set[int]:
    """The editor lines a take has written and not yet had kept."""
    return {
        spec["line"]
        for spec in textarea.decorations
        if spec.get("class") == "cm-line-staged"
    }
