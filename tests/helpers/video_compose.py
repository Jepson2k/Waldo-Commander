"""Compose a recording into video frames.

Each screenshot is framed by a virtual camera, and the pointer, highlights,
keys, captions and title card are drawn over it at the video's own size, so
they stay sharp and keep their size at any zoom.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from waldo_commander.common.theme import hex_of

W, H = 1920, 1080
FPS = 30
_FONT = (
    Path(__file__).resolve().parents[2]
    / "waldo_commander"
    / "static"
    / "fonts"
    / "Geist-Variable.woff2"
)

# Sizes are for the 1920 × 1080 video, picked to read at the tour's 640 × 360.
CAPTION_PX = 52
CAPTION_BOTTOM = 144
CAPTION_INSET = 96
KEY_PX = 88
CURSOR_PX = 56
RIPPLE_FRAMES = round(0.35 * FPS)
FADE_FRAMES = round(0.2 * FPS)
MORPH_FRAMES = round(0.3 * FPS)
#: How far outside what it marks a ring may be drawn, in video pixels,
#: closest last: its band lies between there and just short of the marked
#: thing, so crowded controls get a closer, thinner ring.
RING_PADS = (14, 11, 8, 6)


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @classmethod
    def of(cls, rect: dict) -> Rect:
        """From a WebDriver element's ``rect``."""
        return cls(rect["x"], rect["y"], rect["width"], rect["height"])

    def union(self, other: Rect) -> Rect:
        x0, y0 = min(self.x, other.x), min(self.y, other.y)
        x1 = max(self.x + self.w, other.x + other.w)
        y1 = max(self.y + self.h, other.y + other.h)
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def lerp(self, other: Rect, t: float) -> Rect:
        return Rect(
            *(a + (b - a) * t for a, b in zip(self.box, other.box, strict=True))
        )

    @property
    def box(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.w, self.h

    def corners(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.x + self.w, self.y + self.h


FULL = Rect(0, 0, W, H)


def framing(region: Rect, max_zoom: float = 2.0, margin: float = 0.15) -> Rect:
    """The closest 16:9 view that shows ``region`` with ``margin`` round it,
    zoomed in no further than ``max_zoom``. It stays on the page, so near an
    edge it sits flush with that edge."""
    w = max(
        region.w * (1 + 2 * margin),
        region.h * (1 + 2 * margin) * W / H,
        W / max_zoom,
    )
    w = min(w, W)
    h = w * H / W
    x = region.x + region.w / 2 - w / 2
    y = region.y + region.h / 2 - h / 2
    return Rect(min(max(x, 0), W - w), min(max(y, 0), H - h), w, h)


def ease(t: float) -> float:
    """Cubic ease in and out, clamped to [0, 1]."""
    t = min(max(t, 0.0), 1.0)
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def _between(a: Rect, b: Rect, t: float) -> Rect:
    """A camera move: scale changes evenly in log space, and the centre
    follows it, so zooming in and out feel the same speed. A pan of more
    than half a view widens it on the way, so the page doesn't whip past.
    The view stays on the page."""
    ax, ay = a.x + a.w / 2, a.y + a.h / 2
    bx, by = b.x + b.w / 2, b.y + b.h / 2
    wide = max(a.w, b.w)
    lift = max(0.0, math.log(min(W, math.dist((ax, ay), (bx, by)) + wide / 2) / wide))
    log_w = math.log(a.w) + (math.log(b.w) - math.log(a.w)) * t + lift * 4 * t * (1 - t)
    w = min(W, math.exp(log_w))
    h = w * H / W
    x = min(max(ax + (bx - ax) * t - w / 2, 0.0), W - w)
    y = min(max(ay + (by - ay) * t - h / 2, 0.0), H - h)
    return Rect(x, y, w, h)


@dataclass(frozen=True)
class Camera:
    """A move from ``start`` to ``end`` beginning at frame ``at``."""

    start: Rect = FULL
    end: Rect = FULL
    at: int = 0
    frames: int = 1

    def view(self, n: int) -> Rect:
        return _between(self.start, self.end, ease((n - self.at) / self.frames))


@dataclass(frozen=True)
class Mark:
    """A highlight round ``rect`` from frame ``at``; with ``start`` it
    slides there from another highlight. ``underline`` draws a bar under a
    changing value instead of a ring."""

    rect: Rect
    at: int
    start: Rect | None = None
    underline: bool = False
    #: Drawn faint: where a highlighted thing was before it moved.
    faint: bool = False
    #: How far outside ``rect`` the ring is drawn, in video pixels; a ring
    #: drawn closer is thinner.
    pad: float = 14


@dataclass(frozen=True)
class Keys:
    """Keycaps shown from frame ``at``, laid out in ``rows``, with the keys
    in ``down`` pressed."""

    rows: tuple[tuple[str, ...], ...]
    at: int
    down: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Loupe:
    """Page region ``source`` shown magnified at ``place`` (video pixels),
    from frame ``at``: small controls read up close while the whole page
    stays in view."""

    source: Rect
    place: Rect
    at: int


@dataclass(frozen=True)
class Shot:
    """One video frame: a screenshot and what is drawn over it. Frame numbers
    (``at``) let what is drawn animate across frames that repeat an image."""

    image: bytes
    camera: Camera = Camera()
    pointer: tuple[float, float] | None = None
    pressed: bool = False
    click: tuple[float, float, int] | None = None
    caption: tuple[str, int] | None = None
    #: Where captions and keycaps sit: "bottom" or "top" (centred),
    #: "bottom-left" or "top-left". It is set with the framing, so it holds
    #: still while one framing lasts.
    caption_side: str = "bottom"
    marks: tuple[Mark, ...] = ()
    keys: Keys | None = None
    title: tuple[str, str, int, int | None, int] | None = None
    #: A magnified inset of part of the page.
    loupe: Loupe | None = None
    #: Which mouse button a drag uses, or the wheel: "left", "right", "wheel".
    mouse: str | None = None
    #: What was just taken away, fading out: ("caption", (text, side), frame),
    #: ("marks", marks, frame), ("keys", keys, frame) or ("loupe", loupe,
    #: frame).
    gone: tuple[tuple[str, Any, int], ...] = ()


def _fading(n: int, gone_at: int) -> float:
    """Opacity of something taken away at frame ``gone_at``."""
    return 1 - ease((n - gone_at) / FADE_FRAMES)


def _rgba(token: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    h = hex_of(token).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), round(255 * alpha)


class Composer:
    """Draws shots in order; consecutive frames that would come out the same
    reuse the last one's pixels."""

    def __init__(self) -> None:
        self.fonts: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}
        self.cursor = _cursor(CURSOR_PX)
        self._decoded: tuple[int, Image.Image] | None = None
        self._last: tuple[tuple, bytes] | None = None

    def font(self, size: int, weight: str = "SemiBold") -> ImageFont.FreeTypeFont:
        key = (size, weight)
        if key not in self.fonts:
            font = ImageFont.truetype(str(_FONT), size)
            font.set_variation_by_name(weight)
            self.fonts[key] = font
        return self.fonts[key]

    def _image(self, data: bytes) -> Image.Image:
        if self._decoded is None or self._decoded[0] != id(data):
            self._decoded = (id(data), Image.open(io.BytesIO(data)).convert("RGB"))
        return self._decoded[1]

    def compose(self, n: int, shot: Shot) -> bytes:
        """The frame's raw RGB pixels."""
        view = shot.camera.view(n)
        signature = self._signature(n, shot, view)
        if self._last is not None and self._last[0] == signature:
            return self._last[1]
        source = self._image(shot.image)
        scale = source.width / W
        if view == FULL and scale == 1:
            frame = source.convert("RGBA")
        else:
            box = tuple(c * scale for c in view.corners())
            frame = source.resize((W, H), Image.Resampling.BICUBIC, box=box).convert(
                "RGBA"
            )
        zoom = W / view.w

        def at(x: float, y: float) -> tuple[float, float]:
            return (x - view.x) * zoom, (y - view.y) * zoom

        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for mark in shot.marks:
            self._mark(draw, mark, n, at)
        for kind, value, gone_at in shot.gone:
            if kind == "marks":
                for mark in value:
                    self._mark(draw, mark, n, at, _fading(n, gone_at))
        if shot.pressed and shot.pointer is not None:
            x, y = at(*shot.pointer)
            draw.ellipse((x - 32, y - 32, x + 32, y + 32), fill=_rgba("action", 0.35))
        if shot.click is not None and 0 <= n - shot.click[2] <= RIPPLE_FRAMES:
            self._ripple(draw, at(shot.click[0], shot.click[1]), n - shot.click[2])
        frame.alpha_composite(layer)
        for kind, value, gone_at in shot.gone:
            if kind == "loupe":
                self._loupe(frame, source, shot, value, n, _fading(n, gone_at))
        if shot.loupe is not None:
            self._loupe(
                frame,
                source,
                shot,
                shot.loupe,
                n,
                ease((n - shot.loupe.at) / FADE_FRAMES),
            )
        self._words(frame, shot, n)
        if shot.pointer is not None:
            x, y = at(*shot.pointer)
            if shot.mouse is not None:
                self._mouse(frame, shot.mouse, x + 34, y + 30)
            frame.alpha_composite(self.cursor, (round(x) - 6, round(y) - 5))
        if shot.title is not None:
            self._title(frame, shot.title, n)
        pixels = frame.convert("RGB").tobytes()
        self._last = (signature, pixels)
        return pixels

    def _signature(self, n: int, shot: Shot, view: Rect) -> tuple:
        """Everything the frame's pixels depend on, with each animation's age
        clamped once it has finished."""

        def age(at: int, frames: int) -> int:
            return min(max(n - at, -1), frames)

        return (
            id(shot.image),
            tuple(round(c, 2) for c in view.box),
            shot.pointer,
            shot.pressed,
            shot.click and (shot.click, age(shot.click[2], RIPPLE_FRAMES + 1)),
            shot.caption and (shot.caption[0], age(shot.caption[1], FADE_FRAMES)),
            shot.caption_side,
            tuple(
                (kind, value, age(at, FADE_FRAMES))
                for kind, value, at in shot.gone
                if n - at < FADE_FRAMES
            ),
            tuple((m, age(m.at, MORPH_FRAMES)) for m in shot.marks),
            shot.keys and (shot.keys, age(shot.keys.at, FADE_FRAMES)),
            shot.loupe and (shot.loupe, age(shot.loupe.at, FADE_FRAMES)),
            shot.mouse,
            shot.title
            and (
                shot.title,
                age(shot.title[2], max(FADE_FRAMES, shot.title[4])),
                shot.title[3] is not None and age(shot.title[3], 12),
            ),
        )

    def _mark(self, draw, mark: Mark, n: int, at, fade: float = 1.0) -> None:
        t = ease((n - mark.at) / MORPH_FRAMES)
        rect = mark.start.lerp(mark.rect, t) if mark.start is not None else mark.rect
        alpha = (
            (1.0 if mark.start is not None else t) * fade * (0.35 if mark.faint else 1)
        )
        if alpha <= 0:
            return
        x0, y0 = at(rect.x, rect.y)
        x1, y1 = at(rect.x + rect.w, rect.y + rect.h)
        if mark.underline:
            # Close under it: lines of code leave little room before the next.
            y = y1 + 2
            draw.rounded_rectangle(
                (x0, y, x1, y + 5), radius=2, fill=_rgba("action", alpha)
            )
            return
        pad = mark.pad
        dark = round(pad - 2)
        box = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        draw.rounded_rectangle(
            box, radius=pad + 2, outline=_rgba("scene-bg", 0.8 * alpha), width=dark
        )
        inset = dark / 4
        draw.rounded_rectangle(
            (box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset),
            radius=pad + 2 - inset,
            outline=_rgba("action", alpha),
            width=max(2, round(dark / 2)),
        )

    def _loupe(
        self,
        frame: Image.Image,
        source: Image.Image,
        shot: Shot,
        loupe: Loupe,
        n: int,
        alpha: float,
    ) -> None:
        """The inset, with the pointer and a click drawn in it where they
        fall inside its region."""
        if alpha <= 0:
            return
        src, dst = loupe.source, loupe.place
        scale = source.width / W
        size = (round(dst.w), round(dst.h))
        inset = source.resize(
            size,
            Image.Resampling.BICUBIC,
            box=tuple(c * scale for c in src.corners()),
        ).convert("RGBA")
        zoom = dst.w / src.w

        def inside(x: float, y: float) -> tuple[float, float] | None:
            if not (src.x <= x <= src.x + src.w and src.y <= y <= src.y + src.h):
                return None
            return (x - src.x) * zoom, (y - src.y) * zoom

        marks = ImageDraw.Draw(inset)
        if shot.click is not None and 0 <= n - shot.click[2] <= RIPPLE_FRAMES:
            spot = inside(shot.click[0], shot.click[1])
            if spot is not None:
                self._ripple(marks, spot, n - shot.click[2])
        if shot.pointer is not None:
            spot = inside(*shot.pointer)
            if spot is not None:
                inset.alpha_composite(
                    self.cursor, (round(spot[0]) - 6, round(spot[1]) - 5)
                )
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, *size), radius=18, fill=255)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        layer.paste(inset, (round(dst.x), round(dst.y)), mask)
        ImageDraw.Draw(layer).rounded_rectangle(
            dst.corners(), radius=18, outline=_rgba("action"), width=5
        )
        if alpha < 1:
            layer.putalpha(layer.getchannel("A").point(lambda v: round(v * alpha)))
        frame.alpha_composite(layer)

    def _mouse(self, frame: Image.Image, kind: str, x: float, y: float) -> None:
        """A small mouse beside the pointer, its button or wheel lit."""
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        w, h = 44, 64
        body = (x, y, x + w, y + h)
        draw.rounded_rectangle(body, radius=20, fill=_rgba("surface", 0.95))
        draw.rounded_rectangle(body, radius=20, outline=_rgba("text"), width=3)
        draw.line((x, y + 26, x + w, y + 26), fill=_rgba("text"), width=3)
        draw.line((x + w / 2, y, x + w / 2, y + 26), fill=_rgba("text"), width=3)
        if kind in ("left", "right"):
            left = kind == "left"
            draw.rounded_rectangle(
                (
                    x + (4 if left else w / 2 + 2),
                    y + 4,
                    x + (w / 2 - 2 if left else w - 4),
                    y + 23,
                ),
                radius=8,
                fill=_rgba("action"),
            )
        else:
            draw.rounded_rectangle(
                (x + w / 2 - 5, y + 6, x + w / 2 + 5, y + 22),
                radius=4,
                fill=_rgba("action"),
            )
        frame.alpha_composite(layer)

    def _ripple(self, draw, xy: tuple[float, float], age: int) -> None:
        t = age / RIPPLE_FRAMES
        r = (20 + 68 * (1 - (1 - t) ** 3)) / 2
        x, y = xy
        draw.ellipse(
            (x - r, y - r, x + r, y + r),
            outline=_rgba("action", 0.9 * (1 - t)),
            width=5,
        )

    def _words(self, frame: Image.Image, shot: Shot, n: int) -> None:
        """The caption and keycaps, with whatever was just taken away fading
        out where it stood."""
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for kind, value, gone_at in shot.gone:
            out = _fading(n, gone_at)
            if out <= 0:
                continue
            if kind == "caption":
                # Where it was shown, though the framing has moved it on.
                text, side = value
                self._caption(draw, side, text, out)
            elif kind == "keys":
                self._keys(draw, shot.caption_side, value, n, out)
        if shot.caption is not None:
            fade = ease((n - shot.caption[1]) / FADE_FRAMES)
            self._caption(draw, shot.caption_side, shot.caption[0], fade)
        if shot.keys is not None:
            self._keys(draw, shot.caption_side, shot.keys, n, None)
        frame.alpha_composite(layer)

    def _caption(self, draw, side: str, text: str, fade: float) -> None:
        """A caption on its pill: at the top of the frame or, at the bottom,
        clear of the player's controls; centred, or in from the left."""
        if fade <= 0:
            return
        font = self.font(CAPTION_PX)
        left, top, right, bottom = font.getbbox(text)
        w, h = right - left + 64, CAPTION_PX + 36
        x = (W - w) / 2 if side in ("bottom", "top") else CAPTION_INSET
        y = 96 if side.startswith("top") else H - CAPTION_BOTTOM - h
        draw.rounded_rectangle(
            (x, y, x + w, y + h), radius=20, fill=_rgba("scene-bg", 0.82 * fade)
        )
        draw.text(
            (x + 32 - left, y + (h - (bottom - top)) / 2 - top),
            text,
            font=font,
            fill=_rgba("text", fade),
        )

    def _keys(self, draw, side: str, keys: Keys, n: int, fade: float | None) -> None:
        """Keycaps beside the caption's place, under it at the top of the
        frame and over it at the bottom, whether a caption is up or not: they
        hold still while captions come and go."""
        key_w, key_h = self._keys_size(keys)
        h = CAPTION_PX + 36
        x = (W - key_w) / 2 if side in ("bottom", "top") else CAPTION_INSET
        y = (
            96 + h + 20
            if side.startswith("top")
            else H - CAPTION_BOTTOM - h - 20 - key_h
        )
        self._draw_keys(draw, keys, n, x, y, fade)

    def _key_w(self, key: str) -> float:
        """A letter's keycap is square; a named key's is as wide as its name."""
        if len(key) == 1:
            return KEY_PX
        left, _, right, _ = self.font(44).getbbox(key)
        return max(KEY_PX, right - left + 48)

    def _keys_size(self, keys: Keys | None) -> tuple[float, float]:
        if keys is None:
            return 0, 0
        widest = max(
            sum(self._key_w(k) + 12 for k in row) - 12 + r * (KEY_PX + 12) * 0.3
            for r, row in enumerate(keys.rows)
        )
        return widest, len(keys.rows) * (KEY_PX + 12) - 12

    def _draw_keys(
        self, draw, keys: Keys, n: int, x: float, y: float, fade: float | None
    ) -> None:
        if fade is None:
            fade = ease((n - keys.at) / FADE_FRAMES)
        font = self.font(44)
        for r, row in enumerate(keys.rows):
            # Each row sits a little right of the one above, as on a keyboard.
            kx = x + r * (KEY_PX + 12) * 0.3
            for key in row:
                down = key in keys.down
                kw = self._key_w(key)
                ky = y + r * (KEY_PX + 12) + (4 if down else 0)
                fill = _rgba("action" if down else "surface", fade)
                draw.rounded_rectangle(
                    (kx, ky, kx + kw, ky + KEY_PX),
                    radius=14,
                    fill=fill,
                    outline=_rgba("text-muted", 0.5 * fade),
                    width=2,
                )
                left, top, right, bottom = font.getbbox(key)
                draw.text(
                    (
                        kx + (kw - (right - left)) / 2 - left,
                        ky + (KEY_PX - (bottom - top)) / 2 - top,
                    ),
                    key,
                    font=font,
                    fill=_rgba("on-bright" if down else "text", fade),
                )
                kx += kw + 12

    def _title(
        self,
        frame: Image.Image,
        title: tuple[str, str, int, int | None, int],
        n: int,
    ) -> None:
        heading, text, at, out_at, fade_in = title
        alpha = ease((n - at) / fade_in) if fade_in else 1.0
        if out_at is not None:
            alpha = 1 - ease((n - out_at) / 12)
        if alpha <= 0:
            return
        layer = Image.new("RGBA", (W, H), _rgba("scene-bg", alpha))
        draw = ImageDraw.Draw(layer)
        big, small = self.font(64), self.font(36, "Regular")
        for line, font, y, token in (
            (heading, big, H / 2 - 56, "text"),
            (text, small, H / 2 + 28, "text-muted"),
        ):
            left, top, right, bottom = font.getbbox(line)
            draw.text(
                ((W - (right - left)) / 2 - left, y - top),
                line,
                font=font,
                fill=_rgba(token, alpha),
            )
        frame.alpha_composite(layer)


def _cursor(height: int) -> Image.Image:
    """An arrow pointer, white with a dark edge and a soft shadow."""
    k = 4
    s = height / 21
    outline = [
        (0, 0),
        (0, 17),
        (4.2, 13.2),
        (7, 19.5),
        (9.6, 18.4),
        (6.9, 12.3),
        (12.6, 12.3),
    ]
    pts = [((x * s + 6) * k, (y * s + 5) * k) for x, y in outline]
    size = (round((12.6 * s + 16) * k), round((21 * s + 14) * k))
    big = Image.new("RGBA", size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(
        [(x + 2 * k, y + 3 * k) for x, y in pts], fill=(0, 0, 0, 120)
    )
    big.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(3 * k)))
    draw = ImageDraw.Draw(big)
    draw.polygon(pts, fill=_rgba("scene-bg"))
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    inset = 3 * k

    def inward(p: tuple[float, float]) -> tuple[float, float]:
        dx, dy = p[0] - cx, p[1] - cy
        d = math.hypot(dx, dy) or 1
        return p[0] - dx / d * inset, p[1] - dy / d * inset

    draw.polygon([inward(p) for p in pts], fill=_rgba("text"))
    return big.resize((size[0] // k, size[1] // k), Image.Resampling.LANCZOS)
