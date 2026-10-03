"""Waldo, the robot face: its SVGs and the guest appearances around the app.

The status footer's face is the primary one (see ``readout.StatusFooter``);
:func:`waldo` puts another, independent face anywhere else. Every face
animates in ``static/js/robot-faces.js``.
"""

import json
from enum import Enum
from pathlib import Path

import waldoctl
from nicegui import ui


class RobotFace(Enum):
    """Robot face moods: connected, simulator, disconnected."""

    HAPPY = "happy"
    NEUTRAL = "neutral"
    SAD = "sad"


# Inlined rather than <img>: the face draws in the page's tokens
# (currentColor, --face-cut), which an image cannot reach.
_ICONS_DIR = Path(__file__).parent.parent / "static" / "icons"
FACE_SVGS = {
    face: (_ICONS_DIR / f"robot_{face.value}.svg").read_text() for face in RobotFace
}


def current_mood() -> RobotFace:
    """The mood for the robot's connection: simulator, connected or not."""
    status = waldoctl.commander.status
    if status.simulator_active:
        return RobotFace.NEUTRAL
    return RobotFace.HAPPY if status.connected else RobotFace.SAD


def face_js(call: str) -> str:
    """Wrap a robot-faces.js call so it waits for the deferred script to load."""
    return (
        "(function go(n){if(window.WaldoFace){" + call + "}"
        "else if(n>0){setTimeout(function(){go(n-1)},50)}})(60);"
    )


def mount_js(root: ui.element, mood: RobotFace, **opts: object) -> str:
    """The ``WaldoFace.mount`` call that brings the face inside *root* to life."""
    return face_js(
        f"WaldoFace.mount({json.dumps(root.html_id)}, "
        f"{json.dumps(mood.value)}, {json.dumps(opts)});"
    )


def waldo(
    mood: RobotFace,
    *,
    size: int,
    color: str = "text",
    cut: str = "glass-end",
    idles: bool = True,
    hold: dict[str, bool] | None = None,
    react: str | None = None,
) -> ui.element:
    """A guest Waldo, *size* px square, drawn in the colour token *color*
    with its eyes and mouth cut out in the token *cut* (the surface behind
    it). *hold* sets held states such as ``{"estop": True}``; *react* plays
    one reaction once the face is up."""
    root = (
        ui.element("div")
        .classes(f"robot-face robot-face-{mood.value} waldo-guest text-wc-{color}")
        .style(f"width: {size}px; height: {size}px; --face-cut: var(--wc-{cut})")
    )
    with root:
        ui.html(FACE_SVGS[mood], sanitize=False)
    opts: dict[str, object] = {"idles": idles}
    if hold:
        opts["hold"] = hold
    if react:
        opts["react"] = react
    root.client.run_javascript(mount_js(root, mood, **opts))
    return root


def react(root: ui.element, kind: str) -> None:
    """Play the reaction *kind* on the guest Waldo *root*."""
    if root.is_deleted:
        return
    root.client.run_javascript(
        face_js(f"WaldoFace.react({json.dumps(root.html_id)}, {json.dumps(kind)});")
    )


def peek(root: ui.element, kind: str) -> None:
    """Raise the guest Waldo *root* (a ``waldo-peek`` window) into view,
    play the reaction *kind*, and lower it again."""
    if root.is_deleted:
        return
    root.client.run_javascript(
        face_js(f"WaldoFace.peek({json.dumps(root.html_id)}, {json.dumps(kind)});")
    )
