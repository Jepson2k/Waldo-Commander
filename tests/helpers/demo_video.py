"""Frame-stepped review videos and tutorials.

Each frame is the app settled after one small step of the action (a pointer
move, a key), captured, and the frames are played back at a steady rate. A
machine too slow to capture in real time still records smooth motion, and a
viewer sees every step of a drag instead of the few a wall-clock recorder
catches. What the app does on its own is recorded from the browser's own
frames, and can be slowed while it is recorded and sped back up after.

The pointer, captions, highlights, keys and the framing are not part of the
page: they are drawn over the screenshots when the video is put together
(see ``video_compose``).
"""

from __future__ import annotations

import base64
import json
import math
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Any
from urllib.request import urlopen

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys as K

from tests.helpers.browser_helpers import tap
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle
from tests.helpers.video_compose import (
    FPS,
    FADE_FRAMES,
    FULL,
    MORPH_FRAMES,
    RING_PADS,
    H,
    W,
    Camera,
    Composer,
    Keys,
    Loupe,
    Mark,
    Rect,
    Shot,
    ease,
    framing,
)

if TYPE_CHECKING:
    from nicegui.testing import Screen

#: The browser's input API's button masks.
_BUTTONS = {"left": 1, "right": 2}
#: What a keycap says for a selenium key that is not a character.
_KEY_LABELS = {
    K.CONTROL: "Ctrl",
    K.SHIFT: "Shift",
    K.ALT: "Alt",
    K.ESCAPE: "Esc",
    K.ENTER: "Enter",
    K.DELETE: "Del",
    K.BACKSPACE: "⌫",
    K.TAB: "Tab",
    K.SPACE: "Space",
    K.DOWN: "↓",
    K.UP: "↑",
}


def key_label(key: str) -> str:
    """What the keycap for ``key`` says."""
    return _KEY_LABELS.get(key, key.upper())


#: How much slower than the video real time runs while a demo records.
SLOWDOWN = 10
ROOT = Path(".scene-evidence") / "demos"

Target = Any
"""A page region: a ``Rect``, a WebElement, or a sequence of them."""


#: What an element draws: its text, shapes, images and filled or bordered
#: boxes, clipped where an ancestor clips them. Layout boxes, padding and
#: an SVG's empty corners don't count. ``drawn(el, clip, skip)`` yields
#: ``{el, rect}`` or ``{el, pts}`` (an SVG shape's outline) pieces.
_DRAWN_PRELUDE = """
const ALL = {l: -1e9, t: -1e9, r: 1e9, b: 1e9};
const clear = c => !c || c === "transparent" || /rgba\\([^)]*,\\s*0(\\.0+)?\\)$/.test(c);
const boxPainted = cs => {
  if (!clear(cs.backgroundColor) || cs.backgroundImage !== "none") return true;
  for (const s of ["Top", "Right", "Bottom", "Left"])
    if (parseFloat(cs[`border${s}Width`]) > 0 && cs[`border${s}Style`] !== "none"
        && !clear(cs[`border${s}Color`])) return true;
  return false;
};
const pseudoPainted = el => ["::before", "::after"].some(p => {
  const cs = getComputedStyle(el, p);
  return cs.content && cs.content !== "none" && cs.display !== "none" && boxPainted(cs);
});
// A field's text, where the browser draws it in the field's box.
const typed = (el, cs, r) => {
  const ctx = (typed.ctx ??= document.createElement("canvas").getContext("2d"));
  ctx.font = cs.font;
  const left = r.left + parseFloat(cs.paddingLeft) + parseFloat(cs.borderLeftWidth);
  const room = r.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
    - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth);
  const w = Math.min(ctx.measureText(el.value || el.placeholder).width, room);
  const x = cs.textAlign === "center" ? left + (room - w) / 2
    : cs.textAlign === "right" || cs.textAlign === "end" ? left + room - w : left;
  const half = parseFloat(cs.fontSize) * 0.6, mid = (r.top + r.bottom) / 2;
  return {l: x, t: mid - half, r: x + w, b: mid + half};
};
// A marked field is its native input; what it draws is the whole field.
const widen = el => el.matches("input, textarea, .q-field__native")
  && el.closest(".q-field") || el;
const cut = (r, c) => {
  const o = {l: Math.max(r.left ?? r.l, c.l), t: Math.max(r.top ?? r.t, c.t),
             r: Math.min(r.right ?? r.r, c.r), b: Math.min(r.bottom ?? r.b, c.b)};
  return o.r > o.l && o.b > o.t ? o : null;
};
// Never drawn themselves: what SVG shapes refer to.
const UNDRAWN = new Set(["defs", "clipPath", "mask", "symbol", "marker", "pattern",
  "linearGradient", "radialGradient", "filter", "title", "desc", "style", "script"]);
// Where an SVG clip path lets ``el`` draw, on the page.
const clipped = (el, clip) => {
  const id = /url\\(["']?#([^"')]+)/.exec(getComputedStyle(el).clipPath || "")?.[1];
  const path = id && document.getElementById(id);
  const m = el.getScreenCTM();
  if (!path || !m) return clip;
  let box = null;
  for (const shape of path.children) {
    if (!(shape instanceof SVGGraphicsElement)) continue;
    const b = shape.getBBox();
    for (const [x, y] of [[b.x, b.y], [b.x + b.width, b.y], [b.x, b.y + b.height],
                          [b.x + b.width, b.y + b.height]]) {
      const q = new DOMPoint(x, y).matrixTransform(m);
      box = box ? {l: Math.min(box.l, q.x), t: Math.min(box.t, q.y),
                   r: Math.max(box.r, q.x), b: Math.max(box.b, q.y)}
                : {l: q.x, t: q.y, r: q.x, b: q.y};
    }
  }
  return box ? cut(box, clip) ?? {l: 0, t: 0, r: 0, b: 0} : clip;
};
// ``alpha`` is how opaque the ancestors leave it: a faded control is drawn,
// but dimmed out of the way.
function* drawn(el, clip = ALL, skip = () => false, alpha = 1) {
  if (skip(el) || UNDRAWN.has(el.tagName)) return;
  const cs = getComputedStyle(el);
  if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) return;
  alpha *= parseFloat(cs.opacity);
  if (el instanceof SVGElement) clip = clipped(el, clip);
  if (el instanceof SVGGeometryElement) {
    const m = el.getScreenCTM();
    const bare = (paint, opacity) => paint === "none" || clear(paint) || parseFloat(opacity) === 0;
    if (!m || (bare(cs.fill, cs.fillOpacity) && bare(cs.stroke, cs.strokeOpacity))) return;
    const len = el.getTotalLength(), pts = [];
    for (let i = 0; i <= 64; i++) {
      const p = el.getPointAtLength(len * i / 64), q = new DOMPoint(p.x, p.y).matrixTransform(m);
      if (q.x >= clip.l && q.x <= clip.r && q.y >= clip.t && q.y <= clip.b) pts.push([q.x, q.y]);
    }
    if (pts.length) yield {el, pts, alpha};
    return;
  }
  const rect = el.getBoundingClientRect();
  const html = !(el instanceof SVGElement);
  if ((el.tagName === "INPUT" || el.tagName === "TEXTAREA") && (el.value || el.placeholder)) {
    const c = cut(typed(el, cs, rect), clip);
    if (c) yield {el, rect: c, alpha};
  } else if (["IMG", "CANVAS", "VIDEO"].includes(el.tagName) || el instanceof SVGTextElement
      || (html && (boxPainted(cs) || pseudoPainted(el)))) {
    const c = cut(rect, clip);
    if (c) yield {el, rect: c, alpha};
  }
  if (el instanceof SVGTextElement) return;
  const clips = html ? cs.overflowX !== "visible" || cs.overflowY !== "visible"
    : el instanceof SVGSVGElement && cs.overflow !== "visible";
  const inner = clips ? cut(rect, clip) : clip;
  if (!inner) return;
  for (const n of el.childNodes) {
    if (n.nodeType === 3 && n.textContent.trim()) {
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const q of range.getClientRects()) {
        const c = cut(q, inner);
        if (c) yield {el, rect: c, alpha};
      }
    } else if (n.nodeType === 1) {
      yield* drawn(n, inner, skip, alpha);
    }
  }
}
"""

_DRAWN_JS = (
    _DRAWN_PRELUDE
    + """
const o = {l: Infinity, t: Infinity, r: -Infinity, b: -Infinity};
for (const root of arguments[0]) for (const p of drawn(widen(root))) {
  for (const [x, y] of p.pts ?? [[p.rect.l, p.rect.t], [p.rect.r, p.rect.b]]) {
    o.l = Math.min(o.l, x); o.t = Math.min(o.t, y);
    o.r = Math.max(o.r, x); o.b = Math.max(o.b, y);
  }
}
return o.l === Infinity ? null : [o.l, o.t, o.r - o.l, o.b - o.t];
"""
)

#: What a ring's band (inside ``outer``, outside ``inner``) would be drawn
#: over: the drawn things, other than the marked elements, the scene's
#: canvas and backgrounds that hold the whole ring, that it covers.
_UNDER_RING_JS = (
    _DRAWN_PRELUDE
    + """
const [outer, inner, roots] = arguments;
const edge = {l: outer.l + 2, t: outer.t + 2, r: outer.r - 2, b: outer.b - 2};
const marked = roots.map(widen);
const isMarked = el => marked.some(m => m === el || m.contains(el));
// A ring round something inside a menu or card meets that container's edge.
const holds = el => marked.some(m => el !== m && el.contains(m));
const skip = el => isMarked(el) || el.tagName === "CANVAS";
const within = (x, y, r) => x >= r.l && x <= r.r && y >= r.t && y <= r.b;
const seen = (el, x, y) => {
  const top = document.elementFromPoint(x, y);
  return !top || top === el || el.contains(top) || top.contains(el);
};
const name = el => {
  const text = (el.textContent || "").trim().slice(0, 30);
  const cls = [...el.classList].slice(0, 2).map(c => "." + c).join("");
  return `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${cls}${text ? ` "${text}"` : ""}`;
};
const under = new Set();
for (const p of drawn(document.body, ALL, skip)) {
  if (p.alpha < 0.5 || holds(p.el)) continue;
  if (p.pts) {
    const hit = p.pts.find(([x, y]) => within(x, y, outer) && !within(x, y, inner));
    if (hit && seen(p.el, ...hit)) under.add(name(p.el));
    continue;
  }
  const r = p.rect;
  if (r.l <= outer.l && r.t <= outer.t && r.r >= outer.r && r.b >= outer.b) continue;
  // A hairline (a tab's underline, a divider) or a box that only meets the
  // ring's soft outer edge isn't covered by it.
  if (Math.min(r.r - r.l, r.b - r.t) <= 2.5) continue;
  const c = cut(r, edge);
  if (!c || (c.l >= inner.l && c.r <= inner.r && c.t >= inner.t && c.b <= inner.b)) continue;
  if (seen(p.el, (c.l + c.r) / 2, (c.t + c.b) / 2)) under.add(name(p.el));
}
return [...under];
"""
)


def _elements(target: Target) -> list:
    """The page elements in ``target``."""
    if target is None or isinstance(target, Rect):
        return []
    if isinstance(target, Sequence):
        return [e for t in target for e in _elements(t)]
    return [target]


def region(target: Target) -> Rect:
    """The page rectangle ``target`` covers: for an element, what it draws."""
    if isinstance(target, Rect):
        return target
    if isinstance(target, Sequence):
        rects = [region(t) for t in target]
        out = rects[0]
        for r in rects[1:]:
            out = out.union(r)
        return out
    box = target.parent.execute_script(_DRAWN_JS, [target])
    return Rect.of(target.rect) if box is None else Rect(*box)


def _ease(t: float) -> float:
    """Smooth start and stop."""
    return t * t * (3 - 2 * t)


class _Screencast:
    """The browser's own frames, taken on a thread while the test drives
    the app, each stamped with the time it was drawn."""

    def __init__(self, screen: Screen) -> None:
        import websocket

        address = screen.selenium.capabilities["goog:chromeOptions"]["debuggerAddress"]
        with urlopen(f"http://{address}/json", timeout=5) as response:
            pages = json.load(response)
        page = next(
            p
            for p in pages
            if p.get("type") == "page" and p.get("url") == screen.selenium.current_url
        )
        self.connection = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=0.2, suppress_origin=True
        )
        self.shots: list[tuple[float, bytes]] = []
        self.serial = 1
        self.stopping = threading.Event()
        self._send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 90, "maxWidth": W, "maxHeight": H},
        )
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _send(self, method: str, params: dict | None = None) -> None:
        self.serial += 1
        self.connection.send(
            json.dumps({"id": self.serial, "method": method, "params": params or {}})
        )

    def _run(self) -> None:
        import websocket

        while not self.stopping.is_set():
            try:
                message = json.loads(self.connection.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if message.get("method") != "Page.screencastFrame":
                continue
            params = message["params"]
            self._send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            self.shots.append(
                (params["metadata"]["timestamp"], base64.b64decode(params["data"]))
            )

    def stop(self) -> list[tuple[float, bytes]]:
        self.stopping.set()
        self.thread.join(5)
        try:
            self._send("Page.stopScreencast")
        finally:
            self.connection.close()
        return self.shots


@dataclass
class _Recording:
    """A stretch recorded from the browser's frames: what was drawn over
    them, and when it changed."""

    start: float
    first: int
    dilation: float
    overlay: dict
    changes: list[tuple[float, dict]] = field(default_factory=list)


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
        self.frames: list[Shot] = []
        self.at = (0.0, 0.0)
        self.pressed = False
        self.button = "left"
        self.overlay: dict = {
            "camera": Camera(),
            "pointer": None,
            "pressed": False,
            "click": None,
            "caption": None,
            "caption_side": "top",
            "marks": (),
            "keys": None,
            "title": None,
            "loupe": None,
            "mouse": None,
            "gone": (),
        }
        self._recording: _Recording | None = None
        #: What the ring marks, followed as the page lays it out anew.
        self._ringed: Target | None = None
        # Real time runs about ten times slower than the video while it is
        # recorded, so the hover grace is stretched by as much; otherwise a
        # handle gives way mid-move that a user's quicker move would keep.
        self.grace = scene_js(
            screen,
            "const h=getElement(S.root).core.pointer.hover,g=h.graceMs;"
            "h.graceMs=g*arguments[0];return g;",
            SLOWDOWN,
        )

    # ---- what is drawn over the page ------------------------------------------

    @property
    def now(self) -> int:
        """The video frame a change made now first shows in."""
        r = self._recording
        if r is None:
            return len(self.frames)
        return r.first + round((time.time() - r.start) / r.dilation * FPS)

    def _set(self, **changes: Any) -> None:
        now = self.now
        before = self.overlay
        gone = [g for g in before["gone"] if now - g[2] < FADE_FRAMES]
        if "caption" in changes and before["caption"] is not None:
            new = changes["caption"]
            if new is None or new[0] != before["caption"][0]:
                gone.append(
                    ("caption", (before["caption"][0], before["caption_side"]), now)
                )
        if "marks" in changes:
            kept = {m.rect for m in changes["marks"]} | {
                m.start for m in changes["marks"] if m.start is not None
            }
            dropped = tuple(m for m in before["marks"] if m.rect not in kept)
            if dropped:
                gone.append(("marks", dropped, now))
        if "keys" in changes and before["keys"] is not None:
            new = changes["keys"]
            if new is None or new.rows != before["keys"].rows:
                gone.append(("keys", before["keys"], now))
        if "loupe" in changes and before["loupe"] is not None:
            if changes["loupe"] != before["loupe"]:
                gone.append(("loupe", before["loupe"], now))
        self.overlay = {**before, **changes, "gone": tuple(gone)}
        if self._recording is not None:
            self._recording.changes.append((time.time(), self.overlay))

    def view(self) -> Rect:
        """Where the framing is at the moment."""
        return self.overlay["camera"].view(self.now)

    def focus(
        self,
        target: Target | None,
        zoom: float = 2.0,
        seconds: float | None = None,
        settle: float = 0.2,
        margin: float = 0.15,
        caption: str | None = None,
    ) -> None:
        """Move the framing to show ``target`` with ``margin`` round it,
        zoomed in no further than ``zoom``; None goes back to the whole page.
        The page holds still while the framing moves, for ``seconds`` (by
        default longer the more the zoom changes and the further it pans).
        Captions sit at ``caption`` while the framing lasts: at the top of
        the whole page and top left of a close-up unless given; one where
        the move puts them elsewhere fades out first."""
        if self._recording is None and self.frames:
            # The page holds still while the framing moves: on what it shows
            # now, not on a click's frame with its tooltip still up.
            self.frame(settle=self._hover_gone)
        start = self.view()
        end = FULL if target is None else framing(region(target), zoom, margin)
        if seconds is None:
            pan = math.dist(
                (start.x + start.w / 2, start.y + start.h / 2),
                (end.x + end.w / 2, end.y + end.h / 2),
            ) / max(start.w, end.w)
            seconds = max(
                0.6 * max(0.75, abs(math.log2(start.w / end.w))), 0.45 + 0.6 * pan
            )
        if caption is None:
            caption = "top" if end == FULL else "top-left"
        if caption != self.overlay["caption_side"] and self.overlay["caption"]:
            self._read()
            self.caption(None)
        self._set(
            camera=Camera(start, end, self.now, max(1, round(seconds * FPS))),
            caption_side=caption,
        )
        self.pause(seconds + settle)

    def _hover_gone(self) -> None:
        """Give a tooltip the pointer has left a moment to go."""
        try:
            wait(self.screen, 1).until(lambda _: no_visible(self.screen, ".q-tooltip"))
        except TimeoutException:
            pass

    def loupe(
        self,
        target: Target | None,
        zoom: float = 3.0,
        place: tuple[float, float] | None = None,
    ) -> None:
        """Show ``target`` at ``zoom`` times its size on the page, at
        ``place`` (the inset's top left, in video pixels) or else just under
        it, the rest of the video still in view; None takes it away."""
        if target is None:
            self._set(loupe=None)
            return
        src = region(target)
        w, h = src.w * zoom, src.h * zoom
        if place is None:
            view = self.view()
            scale = W / view.w
            x = (src.x + src.w - view.x) * scale - w
            y = (src.y + src.h - view.y) * scale + 24
        else:
            x, y = place
        x = min(max(x, 24), W - w - 24)
        y = min(max(y, 24), H - h - 24)
        self._set(loupe=Loupe(src, Rect(x, y, w, h), self.now))

    def mouse_hint(self, kind: str | None) -> None:
        """Show beside the pointer the mouse button a drag uses ("left",
        "right") or the wheel ("wheel"); None hides it."""
        self._set(mouse=kind)

    def highlight(self, target: Target | None) -> None:
        """Ring ``target``, sliding over from what was ringed before; None
        takes the highlights away."""
        self._ringed = target
        if target is None:
            self._set(marks=())
            return
        before = next((m for m in self.overlay["marks"] if not m.underline), None)
        start = None
        if before is not None:
            t = ease((self.now - before.at) / MORPH_FRAMES)
            start = before.start.lerp(before.rect, t) if before.start else before.rect
        self._set(marks=(Mark(region(target), self.now, start),))

    def _ring(self) -> Mark | None:
        return next(
            (m for m in self.overlay["marks"] if not m.underline and not m.faint),
            None,
        )

    def _track_ring(self) -> None:
        """Keep the ring on what it marks as the page lays it out anew: a
        control that grows on hover, a panel that shifts."""
        ring = self._ring()
        if ring is None or not _elements(self._ringed):
            return
        try:
            now = region(self._ringed)
        except StaleElementReferenceException:
            return
        if max(abs(a - b) for a, b in zip(now.box, ring.rect.box, strict=True)) <= 2:
            return
        t = ease((self.now - ring.at) / MORPH_FRAMES)
        was = ring.start.lerp(ring.rect, t) if ring.start else ring.rect
        moved = replace(ring, rect=now, start=was, at=self.now)
        self._set(marks=tuple(moved if m is ring else m for m in self.overlay["marks"]))

    def move_highlight(self, target: Target) -> None:
        """Slide the ring to ``target``, where what it marked has moved,
        leaving a faint ring where it was."""
        ring = next((m for m in self.overlay["marks"] if not m.underline), None)
        was = () if ring is None else (Mark(ring.rect, self.now, faint=True),)
        self.highlight(target)
        self._set(marks=(*was, *self.overlay["marks"]))

    def underline(self, target: Target) -> None:
        """Underline a value that is about to change, beside the ring."""
        ring = tuple(m for m in self.overlay["marks"] if not m.underline)
        self._set(marks=(*ring, Mark(region(target), self.now, underline=True)))

    def keys(self, *rows: str | Sequence[str]) -> None:
        """Show keycaps, a row per string of letters (``"QWE", "ASD"``) or
        list of labels (``["Ctrl", "Z"]``); none hides them. Keys pressed
        through ``key``, ``chord`` and ``hold_key`` light up."""
        self._set(keys=Keys(tuple(tuple(r) for r in rows), self.now) if rows else None)

    def _shows(self, *keys: str) -> bool:
        """Whether a keycap on show is for one of ``keys``."""
        shown = self.overlay["keys"]
        labels = {key_label(k) for k in keys}
        return shown is not None and any(k in labels for row in shown.rows for k in row)

    def _key_down(self, *keys: str) -> None:
        shown = self.overlay["keys"]
        if shown is not None:
            down = frozenset(key_label(k) for k in keys)
            self._set(keys=Keys(shown.rows, shown.at, down))

    # ---- frames -------------------------------------------------------------

    def frame(self, settle: Callable[[], object] | None = None) -> None:
        """Wait for the app to settle, then take one frame. While recording
        live, the browser's own frames are already being taken."""
        if settle is not None:
            settle()
        if self._recording is not None:
            return
        wait_for_scene_idle(self.screen)
        self._track_ring()
        shot = self.screen.selenium.execute_cdp_cmd(
            "Page.captureScreenshot",
            {"format": "jpeg", "quality": 90, "fromSurface": True},
        )
        self.frames.append(Shot(base64.b64decode(shot["data"]), **self.overlay))

    def pause(self, seconds: float) -> None:
        """Hold what is on screen; what is drawn over it carries on. While
        recording live, the app carries on too."""
        r = self._recording
        if r is not None:
            time.sleep(seconds * r.dilation)
            return
        if not self.frames:
            self.frame()
        image = self.frames[-1].image
        self.frames.extend(
            Shot(image, **self.overlay) for _ in range(round(seconds * FPS))
        )

    def _read(self) -> None:
        """Hold until the caption on screen has been read; a tutorial holds
        captions longer than a review demo needs to."""

    def caption(self, text: str | None) -> None:
        """Say what the next steps do; None takes the caption away. A new
        caption in place of another comes in once the old one has gone."""
        at = self.now
        if text and self.overlay["caption"] and self.overlay["caption"][0] != text:
            at += FADE_FRAMES
        self._set(caption=(text, at) if text else None)

    def title(self, heading: str, text: str = "", seconds: float = 2.5) -> None:
        """A title card over the app, held for ``seconds``, then faded out."""
        self._set(title=(heading, text, self.now, None, 0))
        self.frame()
        self.pause(seconds)
        self._uncover()

    def cover(self, heading: str, text: str = "", seconds: float = 0.4) -> None:
        """Fade a card in over the video, so what is behind it can change
        unseen (a new setup), rather than cutting to it."""
        self._set(title=(heading, text, self.now, None, round(seconds * FPS)))
        self.pause(seconds)

    def uncover(self, seconds: float = 1.2) -> None:
        """Hold the card over what is behind it now, then fade it out."""
        self.frame()
        self.pause(seconds)
        self._uncover()

    def _uncover(self) -> None:
        heading, text, *_ = self.overlay["title"]
        self._set(title=(heading, text, self.now - 1, self.now, 0))
        self.pause(0.4)
        self._set(title=None)

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

    def _move(self, x: float, y: float) -> None:
        self.at = (x, y)
        self._set(pointer=(x, y))
        if self.pressed:
            self._mouse("mouseMoved", self.button, _BUTTONS[self.button])
        else:
            self._mouse("mouseMoved", "none", 0)

    def hide_pointer(self) -> None:
        """Take the pointer out of the picture until it next moves."""
        self._set(pointer=None)

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
        r = self._recording
        start = time.time()
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
            if r is not None:
                # Live, nothing waits for a frame to be taken: keep the pace.
                time.sleep(max(0.0, start + k / FPS * r.dilation - time.time()))
            if until is not None and until():
                return True
        return until is None

    def press(self, button: str = "left") -> None:
        self.pressed = True
        self.button = button
        self._set(pressed=True, click=(*self.at, self.now))
        self._mouse("mousePressed", button, _BUTTONS[button], 1)
        self.frame()

    def release(self, settle: Callable[[], object] | None = None) -> None:
        self.pressed = False
        self._mouse("mouseReleased", self.button, 0, 1)
        self._set(pressed=False)
        self.frame(settle)

    def hold(
        self,
        seconds: float,
        settle: Callable[[], object] | None = None,
        dilation: float = 1.0,
    ) -> None:
        """Press and hold the button where the pointer is for ``seconds`` of
        video, recording what the app does meanwhile and until ``settle``
        returns after the release."""
        with self.recording(dilation):
            self.press()
            time.sleep(seconds * dilation)
            self.release(settle)
        self.frame()

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
        self._set(pressed=True, click=(*self.at, self.now))
        self._mouse("mousePressed", "left", 1, 1, modifiers=8)
        self.frame()
        self.pressed = False
        self._mouse("mouseReleased", "left", 0, 1, modifiers=8)
        self._set(pressed=False)
        self.frame(settle)

    def native_click(self, element, settle: Callable[[], object] | None = None) -> None:
        """Click ``element`` through WebDriver, shown where the pointer is: a
        synthetic press on a rail tab doesn't always switch tabs while the
        recording browser is busy drawing the scene."""
        self._set(click=(*self.at, self.now))
        element.click()
        self.frame(settle)

    def quick_click(self, settle: Callable[[], object] | None = None) -> None:
        """A click as quick as a user's, about 60 ms down: no frame between
        press and release, where a press held across a frame's capture would
        count as a hold."""
        x, y = self.at
        self._set(click=(x, y, self.now))
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
        self._set(click=(*self.at, self.now))
        self._mouse("mousePressed", "right", 2, 1)
        self._mouse("mouseReleased", "right", 0, 1)
        self.frame()

    def tap(
        self, to: tuple[float, float], settle: Callable[[], object] | None = None
    ) -> None:
        """Show the contact point while tapping through the browser's touch path."""
        self.at = to
        self._set(pointer=to, pressed=True, click=(*to, self.now))
        self.frame()
        self.pause(0.16)
        tap(self.screen, *to)
        self._set(pressed=False)
        self.frame(settle)

    def type(self, text: str, per_key_s: float = 0.15) -> None:
        """Type ``text`` into what has focus, a key at a time."""
        for key in text:
            self.key(key)
            self.pause(per_key_s)

    def key(self, key: str, settle: Callable[[], object] | None = None) -> None:
        """Press one key (a character or a selenium ``Keys`` value); a
        keycap on show lights up for a moment."""
        shown = self._shows(key)
        if shown:
            self._key_down(key)
        ActionChains(self.screen.selenium).send_keys(key).perform()
        self.frame(settle)
        if shown:
            self.pause(0.35)
            self._key_down()

    def hold_key(
        self,
        key: str,
        seconds: float,
        settle: Callable[[], object] | None = None,
        dilation: float = 1.0,
    ) -> None:
        """Hold a letter key down for ``seconds`` of video, recorded live: a
        held key repeats, and the app acts on it until it comes up, and
        ``settle`` returns."""
        event = {
            "key": key,
            "code": f"Key{key.upper()}",
            "windowsVirtualKeyCode": ord(key.upper()),
        }
        with self.recording(dilation):
            self._key_down(key)
            self.screen.selenium.execute_cdp_cmd(
                "Input.dispatchKeyEvent", {"type": "keyDown", "text": key, **event}
            )
            try:
                time.sleep(seconds * dilation)
            finally:
                self.screen.selenium.execute_cdp_cmd(
                    "Input.dispatchKeyEvent", {"type": "keyUp", **event}
                )
                self._key_down()
            if settle is not None:
                settle()
        self.frame()

    def chord(
        self, modifier: str, key: str, settle: Callable[[], object] | None = None
    ) -> None:
        """Press ``key`` with ``modifier`` held, as Ctrl+Z; keycaps on show
        light up for a moment."""
        shown = self._shows(modifier, key)
        if shown:
            self._key_down(modifier, key)
        ActionChains(self.screen.selenium).key_down(modifier).send_keys(key).key_up(
            modifier
        ).perform()
        self.frame(settle)
        if shown:
            self.pause(0.35)
            self._key_down()

    # ---- real time ----------------------------------------------------------

    @contextmanager
    def recording(self, dilation: float = 1.0) -> Iterator[None]:
        """Record what the app does during the block (the arm driving to a
        typed move, a path drawing in) from the browser's own frames.

        With ``dilation``, the block's real time plays back that many times
        faster: whatever the block slows by as much (the arm's speed) looks
        as it would, with that many more frames to show it."""
        if not self.frames:
            self.frame()
        screencast = _Screencast(self.screen)
        recording = _Recording(time.time(), len(self.frames), dilation, self.overlay)
        self._recording = recording
        try:
            yield
        finally:
            self._recording = None
            end = time.time()
            shots = screencast.stop()
        image = self.frames[-1].image
        k, c = 0, 0
        changes = recording.changes
        overlay = recording.overlay
        for n in range(round((end - recording.start) / dilation * FPS)):
            t = recording.start + n / FPS * dilation
            while k < len(shots) and shots[k][0] <= t:
                image = shots[k][1]
                k += 1
            while c < len(changes) and changes[c][0] <= t:
                overlay = changes[c][1]
                c += 1
            self.frames.append(Shot(image, **overlay))

    def live(self, seconds: float) -> None:
        """Record what the app does on its own for ``seconds`` of real time."""
        with self.recording():
            time.sleep(seconds)

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
        self.root.mkdir(parents=True, exist_ok=True)
        out = self.root / f"{self.name}{suffix}.mp4"
        encoder = subprocess.Popen(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                f"{W}x{H}",
                "-framerate",
                str(FPS),
                "-i",
                "-",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(out),
            ],
            stdin=subprocess.PIPE,
        )
        assert encoder.stdin is not None
        composer = Composer()
        try:
            for n, shot in enumerate(self.frames):
                encoder.stdin.write(composer.compose(n, shot))
        finally:
            encoder.stdin.close()
            encoder.wait(timeout=max(120.0, len(self.frames) * 0.5))
        assert encoder.returncode == 0, f"ffmpeg failed encoding {out}"
        return out


TUTORIALS = Path(".scene-evidence") / "tutorials"

#: How long a caption shows before its step starts: longer captions get
#: longer, and every caption stays up at least ``READ_S + READ_PER_WORD_S``
#: a word in all.
LEAD_S = 1.2
LEAD_LONG_S = 1.6
READ_S = 1.5
READ_PER_WORD_S = 0.35
#: How long the whole window shows after the title, before anything moves.
OPENING_S = 1.2
#: Holds on a step's outcome: longer where a value changes.
OUTCOME_S = 1.2
CHANGE_S = 1.5


class Tutorial(DemoVideo):
    """A tutorial video: a title card, a caption read before each step is
    shown, and a hold at the end. Slower than a review demo, for someone
    seeing the feature for the first time."""

    def __init__(self, screen: Screen, name: str, heading: str, text: str) -> None:
        #: Rings that marked more or less than they should, by caption.
        self.ring_faults: list[str] = []
        self._step = "(before the first step)"
        super().__init__(screen, name, TUTORIALS)
        self.title(heading, text, seconds=1.6)
        # The whole window a moment, before anything moves in on it.
        self.pause(OPENING_S)

    def _read(self) -> None:
        """Hold until the caption on screen has been up long enough to read."""
        caption = self.overlay["caption"]
        if caption is None:
            return
        need = READ_S + READ_PER_WORD_S * len(caption[0].split())
        shown = (len(self.frames) - caption[1]) / FPS
        if shown < need:
            self.pause(need - shown)

    def step(
        self,
        caption: str,
        mark: Target | None = None,
        read: float | None = None,
        keys: Sequence[str | Sequence[str]] = (),
    ) -> None:
        """Say what comes next, ringing ``mark`` and showing ``keys``
        (as for ``keys()``) with it, and hold while it is read."""
        self._step = caption
        self._read()
        self._no_tooltip()
        if mark is not None:
            self.highlight(mark)
        if keys:
            self.keys(*keys)
        self.caption(caption)
        if read is None:
            read = LEAD_S if len(caption.split()) <= 5 else LEAD_LONG_S
        self.pause(read)

    def highlight(self, target: Target | None) -> None:
        """Ring ``target``, as for ``DemoVideo``, as far out as its
        neighbours leave room for; a ring drawn over any of them fails the
        recording."""
        super().highlight(target)
        self._fit_ring()

    def _under_ring(self, ring: Mark, pad: float) -> list[str]:
        """What a ring ``pad`` video pixels out would be drawn over."""
        page_pad = pad * self.overlay["camera"].end.w / W
        r = ring.rect
        inner = {"l": r.x, "t": r.y, "r": r.x + r.w, "b": r.y + r.h}
        outer = {
            "l": r.x - page_pad,
            "t": r.y - page_pad,
            "r": r.x + r.w + page_pad,
            "b": r.y + r.h + page_pad,
        }
        return self.screen.selenium.execute_script(
            _UNDER_RING_JS, outer, inner, _elements(self._ringed)
        )

    def _fit_ring(self) -> None:
        """The farthest-out ring clear of what is beside it."""
        ring = self._ring()
        if ring is None:
            return
        for pad in RING_PADS:
            under = self._under_ring(ring, pad)
            if not under:
                break
        else:
            self._ring_fault(f"drawn over {under}")
        if pad != ring.pad:
            fitted = replace(ring, pad=pad)
            self._set(
                marks=tuple(fitted if m is ring else m for m in self.overlay["marks"])
            )

    def _ring_on_target(self) -> None:
        """The ring still round what it marks, and clear of its neighbours
        as the page now lays them out."""
        if self._ring() is None or not _elements(self._ringed):
            return
        try:
            region(self._ringed)
        except StaleElementReferenceException:
            self._ring_fault("left where what it marked was")
            return
        self._track_ring()
        self._fit_ring()

    def _ring_fault(self, what: str) -> None:
        fault = f"{self._step!r}: ring round {self._ring().rect} {what}"
        if fault not in self.ring_faults:
            self.ring_faults.append(fault)

    def pause(self, seconds: float) -> None:
        """Hold what is on screen; a hold on an outcome has no tooltip left
        over it from the click that made it."""
        if seconds >= OUTCOME_S and self._recording is None:
            self._no_tooltip()
        super().pause(seconds)

    def _no_tooltip(self) -> None:
        """A tooltip the pointer left behind would sit over the next step:
        the script should move the pointer off what it last pointed at."""
        try:
            wait(self.screen, 2).until(lambda _: no_visible(self.screen, ".q-tooltip"))
        except TimeoutException:
            shown = [
                e.text
                for e in self.screen.selenium.find_elements(
                    By.CSS_SELECTOR, ".q-tooltip"
                )
                if e.is_displayed()
            ]
            raise AssertionError(f"a tooltip is up before a step: {shown}") from None
        if self._recording is None:
            # Holds repeat the last frame, which may have been taken while
            # the tooltip was still up.
            self.frame()
        self._ring_on_target()

    def finish(self, wide: bool = True) -> Path:
        """Clear the overlays, pull back to the whole page unless ``wide``
        is False, hold, and save. Rings that marked more or less than they
        should fail it, all of them listed."""
        if self.ring_faults:
            raise AssertionError("rings:\n" + "\n".join(self.ring_faults))
        self._read()
        self.caption(None)
        self.highlight(None)
        self.keys()
        if wide:
            self.focus(None)
        self.pause(2.0)
        return self.save()
