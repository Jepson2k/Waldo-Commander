"""Frame-stepped review videos.

Each frame is the app settled after one small step of the action (a pointer
move, a key), captured, and the frames are played back at a steady rate. A
machine too slow to capture in real time still records smooth motion, and a
viewer sees every step of a drag instead of the few a wall-clock recorder
catches.
"""

from __future__ import annotations

import base64
import json
import math
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING
from urllib.request import urlopen

from selenium.webdriver.common.action_chains import ActionChains

from tests.helpers.browser_helpers import tap
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle

if TYPE_CHECKING:
    from nicegui.testing import Screen

FPS = 30
#: The browser's input API's button masks.
_BUTTONS = {"left": 1, "right": 2}
#: How much slower than the video real time runs while a demo records.
SLOWDOWN = 10
ROOT = Path(".scene-evidence") / "demos"

_OVERLAY = """
if (!window.__demo) {
  const pointer = document.createElement('div');
  pointer.style.cssText = 'position:fixed;left:-40px;top:-40px;width:22px;height:22px;'
    + 'margin:-11px 0 0 -11px;border:2px solid var(--wc-text);border-radius:50%;'
    + 'box-shadow:0 0 0 2px var(--wc-scene-bg);pointer-events:none;z-index:10001;'
    + 'transition:none';
  const caption = document.createElement('div');
  caption.style.cssText = 'position:fixed;left:50%;bottom:72px;transform:translateX(-50%);'
    + 'padding:10px 18px;border-radius:999px;background:var(--wc-surface);'
    + 'color:var(--wc-text);border:1px solid var(--wc-glass-border);'
    + 'font:500 20px/1.3 var(--wc-font-sans);white-space:nowrap;pointer-events:none;'
    + 'z-index:10001';
  caption.hidden = true;
  const title = document.createElement('div');
  title.style.cssText = 'position:fixed;inset:0;display:flex;flex-direction:column;'
    + 'align-items:center;justify-content:center;gap:14px;background:var(--wc-scene-bg);'
    + 'color:var(--wc-text);font-family:var(--wc-font-sans);pointer-events:none;'
    + 'z-index:10002';
  const heading = document.createElement('div');
  heading.style.cssText = 'font:600 48px/1.2 var(--wc-font-sans);letter-spacing:-0.02em';
  const text = document.createElement('div');
  text.style.cssText = 'font:400 22px/1.4 var(--wc-font-sans);color:var(--wc-text-muted)';
  title.append(heading, text);
  title.hidden = true;
  document.body.append(pointer, caption, title);
  window.__demo = {pointer, caption, title, heading, text};
}
"""

_POINT = """
const d = window.__demo, [x, y, pressed] = arguments;
d.pointer.style.left = x + 'px';
d.pointer.style.top = y + 'px';
d.pointer.style.background = pressed ? 'var(--wc-action)' : 'transparent';
"""

_TITLE = """
const d = window.__demo, [heading, text, opacity] = arguments;
d.heading.textContent = heading || '';
d.text.textContent = text || '';
d.title.style.opacity = opacity;
d.title.hidden = !heading;
"""

_CAPTION = """
const c = window.__demo.caption;
c.textContent = arguments[0] || '';
c.hidden = !arguments[0];
"""


def _ease(t: float) -> float:
    """Smooth start and stop."""
    return t * t * (3 - 2 * t)


class DemoVideo:
    """A review video recorded a frame at a time; see the module docstring.

    ``settle`` callables passed to the motion methods run after each step,
    before its frame is taken: they wait for what that step set off (the
    arm catching up, the simulator answering a scrub)."""

    #: The video being recorded, which a failed demo leaves unsaved.
    current: DemoVideo | None = None

    def __init__(self, screen: Screen, name: str, root: Path = ROOT) -> None:
        DemoVideo.current = self
        self.screen = screen
        self.name = name
        self.root = root
        self.frames: list[bytes] = []
        self.at = (0.0, 0.0)
        self.pressed = False
        self.button = "left"
        screen.selenium.execute_script(_OVERLAY)
        # Real time runs about ten times slower than the video while it is
        # recorded, so the hover grace is stretched by as much; otherwise a
        # handle gives way mid-move that a user's quicker move would keep.
        self.grace = scene_js(
            screen,
            "const h=getElement(S.root).core.pointer.hover,g=h.graceMs;"
            "h.graceMs=g*arguments[0];return g;",
            SLOWDOWN,
        )

    # ---- frames -------------------------------------------------------------

    def frame(self, settle: Callable[[], object] | None = None) -> None:
        """Wait for the app to settle, then take one frame."""
        if settle is not None:
            settle()
        wait_for_scene_idle(self.screen)
        shot = self.screen.selenium.execute_cdp_cmd(
            "Page.captureScreenshot",
            {"format": "jpeg", "quality": 90, "fromSurface": True},
        )
        self.frames.append(base64.b64decode(shot["data"]))

    def pause(self, seconds: float) -> None:
        """Hold what is on screen."""
        if not self.frames:
            self.frame()
        self.frames.extend([self.frames[-1]] * round(seconds * FPS))

    def caption(self, text: str | None) -> None:
        """Say what the next steps do; None takes the caption away."""
        self.screen.selenium.execute_script(_CAPTION, text)

    def title(self, heading: str, text: str = "", seconds: float = 2.5) -> None:
        """A title card over the app, held for ``seconds``, then faded out."""
        self.screen.selenium.execute_script(_TITLE, heading, text, 1)
        self.frame()
        self.pause(seconds)
        steps = round(0.5 * FPS)
        for k in range(1, steps + 1):
            self.screen.selenium.execute_script(_TITLE, heading, text, 1 - k / steps)
            self.frame()
        self.screen.selenium.execute_script(_TITLE, None, None, 1)

    # ---- the pointer --------------------------------------------------------

    def _mouse(
        self, kind: str, button: str, buttons: int, clicks: int = 0, modifiers: int = 0
    ) -> None:
        # Through the browser's input API: a WebDriver action sequence ends
        # with no button held, and the scene ends a drag whose button comes
        # up unseen.
        x, y = self.at
        self.screen.selenium.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": kind,
                "x": x,
                "y": y,
                "button": button,
                "buttons": buttons,
                "clickCount": clicks,
                "modifiers": modifiers,
                "pointerType": "mouse",
            },
        )
        self.screen.selenium.execute_script(_POINT, x, y, self.pressed)

    def _move(self, x: float, y: float) -> None:
        self.at = (x, y)
        if self.pressed:
            self._mouse("mouseMoved", self.button, _BUTTONS[self.button])
        else:
            self._mouse("mouseMoved", "none", 0)

    def jump(self, x: float, y: float) -> None:
        """Put the pointer at (x, y) without a frame in between."""
        self._move(x, y)

    def glide(
        self,
        to: tuple[float, float],
        seconds: float,
        settle: Callable[[], object] | None = None,
    ) -> None:
        """Move the pointer to ``to`` over ``seconds``, a frame per step."""
        self.along([self.at, to], seconds, settle)

    def along(
        self,
        points: Sequence[tuple[float, float]],
        seconds: float,
        settle: Callable[[], object] | None = None,
        until: Callable[[], bool] | None = None,
    ) -> bool:
        """Move the pointer through ``points`` at an even pace over
        ``seconds``, easing in and out, a frame per step; with ``until``,
        stop at the first frame where it holds and say whether it did."""
        lengths = [math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)]
        total = sum(lengths) or 1.0
        steps = max(1, round(seconds * FPS))
        for k in range(1, steps + 1):
            d = _ease(k / steps) * total
            i = 0
            while i < len(lengths) - 1 and d > lengths[i]:
                d -= lengths[i]
                i += 1
            f = min(1.0, d / lengths[i]) if lengths[i] else 1.0
            (x0, y0), (x1, y1) = points[i], points[i + 1]
            self._move(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
            self.frame(settle)
            if until is not None and until():
                return True
        return until is None

    def press(self, button: str = "left") -> None:
        self.pressed = True
        self.button = button
        self._mouse("mousePressed", button, _BUTTONS[button], 1)
        self.frame()

    def release(self, settle: Callable[[], object] | None = None) -> None:
        self.pressed = False
        self._mouse("mouseReleased", self.button, 0, 1)
        self.frame(settle)

    def scroll(
        self, delta: float, seconds: float, settle: Callable[[], object] | None = None
    ) -> None:
        """Turn the wheel by ``delta`` pixels (positive: toward the user)
        over ``seconds``, a frame per notch."""
        steps = max(1, round(seconds * FPS))
        x, y = self.at
        for _ in range(steps):
            self.screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": x,
                    "y": y,
                    "deltaX": 0,
                    "deltaY": delta / steps,
                    "pointerType": "mouse",
                },
            )
            self.frame(settle)

    def click(
        self, settle: Callable[[], object] | None = None, *, shift: bool = False
    ) -> None:
        if not shift:
            self.press()
            self.release(settle)
            return
        # The browser's input API spells Shift as modifier bit 8.
        self.pressed = True
        self._mouse("mousePressed", "left", 1, 1, modifiers=8)
        self.frame()
        self.pressed = False
        self._mouse("mouseReleased", "left", 0, 1, modifiers=8)
        self.frame(settle)

    def quick_click(self, settle: Callable[[], object] | None = None) -> None:
        """A click as quick as a user's, about 60 ms down: no frame between
        press and release, where a press held across a frame's capture would
        count as a hold."""
        x, y = self.at
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            if buttons == 0:
                time.sleep(0.06)
            self.screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": kind,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": buttons,
                    "clickCount": 1,
                    "pointerType": "mouse",
                },
            )
        self.frame(settle)

    def context_click(self) -> None:
        self._mouse("mousePressed", "right", 2, 1)
        self._mouse("mouseReleased", "right", 0, 1)
        self.frame()

    def tap(
        self, to: tuple[float, float], settle: Callable[[], object] | None = None
    ) -> None:
        """Show the contact point while tapping through the browser's touch path."""
        self.at = to
        self.screen.selenium.execute_script(_POINT, *to, True)
        self.frame()
        self.pause(0.16)
        tap(self.screen, *to)
        self.screen.selenium.execute_script(_POINT, *to, False)
        self.frame(settle)

    def type(self, text: str, per_key_s: float = 0.15) -> None:
        """Type ``text`` into what has focus, a key at a time."""
        for key in text:
            self.key(key)
            self.pause(per_key_s)

    def key(self, key: str, settle: Callable[[], object] | None = None) -> None:
        """Press one key (a character or a selenium ``Keys`` value)."""
        ActionChains(self.screen.selenium).send_keys(key).perform()
        self.frame(settle)

    def hold_key(self, key: str, seconds: float) -> None:
        """Hold a letter key down for ``seconds`` of real time, recorded live:
        a held key repeats, and the app acts on it until it comes up."""
        event = {
            "key": key,
            "code": f"Key{key.upper()}",
            "windowsVirtualKeyCode": ord(key.upper()),
        }
        self.screen.selenium.execute_cdp_cmd(
            "Input.dispatchKeyEvent", {"type": "keyDown", "text": key, **event}
        )
        try:
            self.live(seconds)
        finally:
            self.screen.selenium.execute_cdp_cmd(
                "Input.dispatchKeyEvent", {"type": "keyUp", **event}
            )
        self.frame()

    def chord(
        self, modifier: str, key: str, settle: Callable[[], object] | None = None
    ) -> None:
        """Press ``key`` with ``modifier`` held, as Ctrl+Z."""
        ActionChains(self.screen.selenium).key_down(modifier).send_keys(key).key_up(
            modifier
        ).perform()
        self.frame(settle)

    # ---- real time ----------------------------------------------------------

    def live(self, seconds: float) -> None:
        """Record what the app does on its own (the arm driving to a typed
        move, a path drawing in) in real time, from the browser's own frames,
        resampled to the video's rate."""
        address = self.screen.selenium.capabilities["goog:chromeOptions"][
            "debuggerAddress"
        ]
        with urlopen(f"http://{address}/json", timeout=5) as response:
            pages = json.load(response)
        page = next(
            p
            for p in pages
            if p.get("type") == "page"
            and p.get("url") == self.screen.selenium.current_url
        )
        import websocket

        connection = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=0.5, suppress_origin=True
        )
        shots: list[tuple[float, bytes]] = []
        serial = 1
        try:
            connection.send(
                json.dumps(
                    {
                        "id": serial,
                        "method": "Page.startScreencast",
                        "params": {
                            "format": "jpeg",
                            "quality": 90,
                            "maxWidth": 1920,
                            "maxHeight": 1080,
                        },
                    }
                )
            )
            start = time.monotonic()
            while time.monotonic() - start < seconds:
                try:
                    message = json.loads(connection.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if message.get("method") != "Page.screencastFrame":
                    continue
                params = message["params"]
                serial += 1
                connection.send(
                    json.dumps(
                        {
                            "id": serial,
                            "method": "Page.screencastFrameAck",
                            "params": {"sessionId": params["sessionId"]},
                        }
                    )
                )
                shots.append(
                    (time.monotonic() - start, base64.b64decode(params["data"]))
                )
            serial += 1
            connection.send(json.dumps({"id": serial, "method": "Page.stopScreencast"}))
        finally:
            connection.close()
        if not shots:
            self.pause(seconds)
            return
        k = 0
        for n in range(round(seconds * FPS)):
            while k + 1 < len(shots) and shots[k + 1][0] <= n / FPS:
                k += 1
            self.frames.append(shots[k][1])

    # ---- output -------------------------------------------------------------

    def save(self, suffix: str = "") -> Path:
        """Encode the frames as ``<root>/<name><suffix>.mp4``."""
        DemoVideo.current = None
        scene_js(
            self.screen,
            "getElement(S.root).core.pointer.hover.graceMs=arguments[0]",
            self.grace,
        )
        ffmpeg = which("ffmpeg")
        assert ffmpeg, "ffmpeg is needed to encode review videos"
        assert self.frames, "a demo with no frames"
        self.caption(None)
        self.root.mkdir(parents=True, exist_ok=True)
        out = self.root / f"{self.name}{suffix}.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "image2pipe",
                "-framerate",
                str(FPS),
                "-c:v",
                "mjpeg",
                "-i",
                "-",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(out),
            ],
            input=b"".join(self.frames),
            check=True,
            timeout=max(120.0, len(self.frames) * 0.5),
        )
        return out


TUTORIALS = Path(".scene-evidence") / "tutorials"


class Tutorial(DemoVideo):
    """A tutorial video: a title card, a caption read before each step is
    shown, and a hold at the end. Slower than a review demo, for someone
    seeing the feature for the first time."""

    def __init__(self, screen: Screen, name: str, heading: str, text: str) -> None:
        super().__init__(screen, name, TUTORIALS)
        self.title(heading, text)

    def step(self, caption: str, read: float = 1.0) -> None:
        """Say what comes next, and hold while it is read."""
        self.caption(caption)
        self.frame()
        self.pause(read)

    def finish(self) -> Path:
        self.caption(None)
        self.frame()
        self.pause(1.5)
        return self.save()
