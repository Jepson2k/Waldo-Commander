"""The 3D scene as browser tests see it.

Every test reaches the scene through ``S``, the surface the scene element
keeps for tests (``scene3d/js/testing.js``): its three.js objects by name,
where they land on screen, what a pointer at a pixel would grab, the camera,
and probes that find a pixel where a press grabs a given handle. Tests never
touch the element's internals.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement

from tests.helpers.browser_helpers import js

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

SCENE_ROOT = ".wc-scene"
SCENE_CANVAS = f"{SCENE_ROOT} canvas"

_PRELUDE = (
    "const S = (() => { const root = document.querySelector('.wc-scene');"
    " const c = root && getElement(root); return c && c.core ? c.core.surface : null; })();\n"
)


def with_surface(body: str) -> str:
    """``body`` as a page script with the scene's surface bound to ``S``."""
    return _PRELUDE + "if (!S) return null;\n" + body


def scene_js(screen: Screen, body: str, *args: Any) -> Any:
    """Run ``body`` in the page with the scene's surface bound to ``S``; null
    while no scene is mounted."""
    return js(screen, with_surface(body), *args)


def scene_js_async(screen: Screen, body: str, *args: Any) -> Any:
    """``scene_js`` for a body that ends by calling ``done(value)``."""
    return screen.selenium.execute_async_script(
        "const done = arguments[arguments.length - 1];\n"
        + _PRELUDE
        + "if (!S) { done(null); return; }\n"
        + body,
        *args,
    )


def scene_canvas(screen: Screen) -> WebElement:
    return screen.selenium.find_element(By.CSS_SELECTOR, SCENE_CANVAS)


def scene_object(screen: Screen, name: str) -> dict | None:
    """The named three.js object's type, visibility and position, or None."""
    return scene_js(
        screen,
        "const o = S.byName(arguments[0]);"
        "return o && {name: o.name, type: o.type, visible: o.visible,"
        " position: {x: o.position.x, y: o.position.y, z: o.position.z}};",
        name,
    )


def wait_for_scene_idle(screen: Screen) -> None:
    """Return once every object the scene has been sent is loaded and drawn."""
    scene_js_async(screen, "S.idle().then(() => done(true));")


def frames(screen: Screen) -> int:
    return scene_js(screen, "return S.frames()")


def frames_at_rest(screen: Screen, window_s: float, timeout: float = 15.0) -> int:
    """The scene's frame count once it has held for *window_s*."""
    deadline = time.monotonic() + timeout
    count = frames(screen)
    since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.1)
        now = frames(screen)
        if now != count:
            count, since = now, time.monotonic()
        elif time.monotonic() - since >= window_s:
            return count
    raise AssertionError(f"the scene kept drawing at rest ({count} frames)")


def pointer_to(screen: Screen, x: float, y: float, actions: ActionChains) -> None:
    """Queue a real pointer move to viewport point (x, y) on ``actions``."""
    canvas = scene_canvas(screen)
    rect = canvas.rect
    actions.move_to_element_with_offset(
        canvas,
        round(x - (rect["x"] + rect["width"] / 2)),
        round(y - (rect["y"] + rect["height"] / 2)),
    )


def project_local(
    screen: Screen, name: str, points: list[list[float]]
) -> list[list[float]] | None:
    """Viewport pixels of ``points`` given in the frame of the scene object
    ``name``, wherever they land."""
    return scene_js(
        screen, "return S.project(arguments[0], arguments[1])", name, points
    )


def scene_object_pixel(
    screen: Screen, name: str, timeout: float = 20.0
) -> tuple[float, float]:
    """A viewport pixel where ``name`` is the first thing a press would hit."""
    deadline = time.monotonic() + timeout
    while True:
        pixel = scene_js(screen, "return S.hoverPixel(arguments[0])", name)
        if pixel is not None:
            return pixel[0], pixel[1]
        if time.monotonic() > deadline:
            raise AssertionError(f"no pixel of {name!r} is hit first on the canvas")
        time.sleep(0.2)


def hover_scene_object(screen: Screen, name: str, timeout: float = 20.0) -> None:
    """Rest the real mouse on ``name``, so the scene reports it as hovered."""
    x, y = scene_object_pixel(screen, name, timeout)
    actions = ActionChains(screen.selenium, duration=0)
    pointer_to(screen, x, y, actions)
    actions.perform()


def emits_during(screen: Screen, action: Callable[[], None]) -> list[str]:
    """The events the page sends the app while *action* runs: a gesture's
    phase (``begin``, ``move``, ``keep``, and ``end`` or ``abort``),
    ``context`` for a menu request, and ``<element id>:<listener id>`` for
    anything else."""
    return [what for what, _ in timed_emits_during(screen, action)]


def timed_emits_during(
    screen: Screen, action: Callable[[], None]
) -> list[tuple[str, float]]:
    """``emits_during``, each event with when it was sent (ms)."""
    js(
        screen,
        "if (!window.__wcEmits) {"
        "  const emit = window.socket.emit.bind(window.socket);"
        "  window.__wcEmits = {on: false, log: []};"
        "  const what = (e) => {"
        "    if (!e) return '?';"
        "    const raw = Array.isArray(e.args) ? e.args[0] : e.args;"
        "    const a = typeof raw === 'string' ? JSON.parse(raw) : raw;"
        "    if (a && typeof a === 'object' && a.t)"
        "      return a.t === 'end' && a.aborted ? 'abort' : a.t;"
        "    if (a && typeof a === 'object' && 'gen' in a) return 'context';"
        "    return e.id + ':' + e.listener_id;"
        "  };"
        "  window.socket.emit = (name, ...rest) => {"
        "    if (name === 'event' && window.__wcEmits.on)"
        "      window.__wcEmits.log.push([what(rest[0]), performance.now()]);"
        "    return emit(name, ...rest);"
        "  };"
        "}"
        "window.__wcEmits.log = []; window.__wcEmits.on = true;",
    )
    try:
        action()
    finally:
        log = js(screen, "window.__wcEmits.on = false; return window.__wcEmits.log;")
    return [(what, t) for what, t in log]


def shown_handles(screen: Screen) -> list[str]:
    """The jog handles on screen: ``ring:<joint>`` and ``gizmo``."""
    return scene_js(screen, "return S.handles()") or []


def snap_deg(screen: Screen) -> float:
    """How far a joint ring snaps, in degrees, at the camera's distance."""
    return scene_js(screen, "return S.snap().joint_deg")
