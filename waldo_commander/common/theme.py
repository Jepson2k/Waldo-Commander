"""Theme: the palettes, every other design token, and the adapters that hand
them to CSS, Quasar, Three.js and charts.

A theme is a :class:`Palette` of fourteen colours (a base16-style scheme with
named roles). Every colour token in the app is either fixed (the simulator
amber, the E-Stop red, the axis hues, the tool teal, the arm greys) or derived
from the active palette, so switching the theme moves every linked role
together: the surfaces, the text, the action colour, the status colours, the
scene floor.

- CSS reads ``var(--wc-<name>)`` (emitted on ``:root``).
- Quasar ``color=`` / ``text-color=`` props use the registered name ``wc-<name>``.
- Three.js and ECharts read hex from :func:`hex_of`; vertex colours use :func:`linear_rgb`.
"""

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from nicegui import app, ui

ThemeKey = Literal["dark", "light"]

STORAGE_KEY = "theme"


@dataclass(frozen=True)
class Palette:
    """The fourteen colours a theme is made of."""

    bg: str
    surface: str
    surface_2: str
    border: str
    text: str
    muted: str
    accent: str
    red: str
    orange: str
    yellow: str
    green: str
    cyan: str
    blue: str
    magenta: str


THEMES: dict[str, Palette] = {
    "Ink": Palette(
        bg="#000000",
        surface="#171717",
        surface_2="#383838",
        border="#858585",
        text="#ffffff",
        muted="#cfcfcf",
        accent="#38bdf8",
        red="#ff5555",
        orange="#ff9f43",
        yellow="#ffd43b",
        green="#4ade80",
        cyan="#22d3ee",
        blue="#60a5fa",
        magenta="#c084fc",
    ),
}
DEFAULT_THEME = "Ink"

#: Colours that mean one thing whatever the theme.
FIXED_COLOR: dict[str, str] = {
    "scene-arm": "#a3a3a3",
    "scene-arm-sim": "#c77d28",
    "scene-arm-edit": "#525252",
    "scene-tool": "#2a9d8f",
    "scene-tool-moving": "#4ecdc4",
    "scene-tool-edit": "#3d6b65",
    "scene-tool-moving-edit": "#4d7e77",
    "scene-collision": "#b00020",
    "scene-shape": "#6d8ea0",
    "scene-shape-draft": "#9db8c8",
    "scene-shape-install": "#55606a",
    "scene-shape-proposed": "#8a7bb5",
    "scene-hover": "#ffffff",
    "axis-x": "#d94c3f",
    "axis-y": "#2faf7a",
    "axis-z": "#4a63e0",
    "axis-rx": "#f1a79f",
    "axis-ry": "#aee5cf",
    "axis-rz": "#aeb9f3",
    "axis-x-text": "oklch(75.6% 0.145 28.7)",
    "axis-y-text": "oklch(72.2% 0.135 160.8)",
    "axis-z-text": "oklch(74.4% 0.13 270.3)",
    "axis-rx-text": "oklch(82.8% 0.089 26.1)",
    "axis-ry-text": "#aee5cf",
    "axis-rz-text": "oklch(81.8% 0.083 276)",
    "mode-sim": "oklch(66.6% 0.179 58.318)",
    "estop": "oklch(63.7% 0.237 25.331)",
    "record": "oklch(63.7% 0.237 25.331)",
    "on-fill": "oklch(98.5% 0 0)",
    "on-bright": "oklch(20.5% 0 0)",
    "scrim": "oklch(0% 0 0 / 0.5)",
    "editor-exec-line": "#ffff004d",
}

# ── Everything that is not a colour ──────────────────────────────────

SPACE: dict[str, str] = {
    "space-1": "4px",
    "space-2": "8px",
    "space-3": "12px",
    "space-4": "16px",
}
RADIUS: dict[str, str] = {
    "radius-sm": "6px",
    "radius-md": "10px",
    "radius-pill": "9999px",
}
SIZE: dict[str, str] = {
    "size-control": "32px",
    "size-control-sm": "24px",
    "size-joint-dial": "64px",
    "size-jog-slot": "72px",
    "size-rail": "52px",
    "size-panel-inset": "58px",
    "size-bottom-panel": "340px",
    "size-column-min": "200px",
    "size-footer": "28px",
}
EFFECT: dict[str, str] = {"glass-blur": "36px", "glass-saturate": "150%"}
OPACITY: dict[str, str] = {"opacity-locked": "0.15"}
Z_INDEX: dict[str, str] = {
    "z-loading": "10",
    "z-cards": "20",
    "z-panels": "30",
    "z-rail": "40",
    "z-rail-bottom": "50",
    "z-glow": "9998",
    "z-capsule": "9999",
}
DURATION: dict[str, str] = {
    "duration-instant": "40ms",
    "duration-fast": "150ms",
    "duration-base": "300ms",
    "duration-flash": "1500ms",
    "duration-ambient": "2600ms",
}
EASING: dict[str, str] = {
    "ease-enter": "ease-out",
    "ease-loop": "ease-in-out",
    "ease-pop": "cubic-bezier(0.34, 1.56, 0.64, 1)",
}
SHADOW: dict[str, str] = {
    "shadow-glass": (
        "inset -0.3px -1px 4px 0 rgba(0, 0, 0, 0.072), inset -1.5px 2.5px 0 -2px rgba(0, 0, 0, 0.108),"
        " inset 0 3px 4px -2px rgba(0, 0, 0, 0.108), 0 6px 16px 0 rgba(0, 0, 0, 0.048)"
    ),
}
FONT_FAMILY: dict[str, str] = {
    "sans": 'Roboto, -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif',
    "mono": 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
}
# name -> (font-size, line-height, weight, letter-spacing or None)
TYPE_STYLE: dict[str, tuple[str, str, int, str | None]] = {
    "title": ("18px", "28px", 500, None),
    "label": ("14px", "20px", 500, None),
    "body": ("14px", "20px", 400, None),
    "caption": ("12px", "16px", 400, None),
    "micro": ("11px", "14px", 500, "0.02em"),
}

# ── Colour maths (sRGB <-> OKLCH) ────────────────────────────────────

_OKLCH = re.compile(
    r"oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)\s*(?:/\s*([\d.]+)(%?))?\s*\)"
)


def _oklch_to_srgb(L: float, C: float, h: float) -> tuple[float, float, float]:
    a = C * math.cos(math.radians(h))
    b = C * math.sin(math.radians(h))
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    lc, mc, sc = l_**3, m_**3, s_**3
    lin = (
        4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc,
        -1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc,
        -0.0041960863 * lc - 0.7034186147 * mc + 1.7076147010 * sc,
    )

    def enc(x: float) -> float:
        x = min(max(x, 0.0), 1.0)
        return 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055

    return enc(lin[0]), enc(lin[1]), enc(lin[2])


def _srgb_eotf(x: float) -> float:
    """An sRGB-encoded channel in 0–1 as linear light."""
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def _srgb_to_oklch(r: float, g: float, b: float) -> tuple[float, float, float]:
    r, g, b = _srgb_eotf(r), _srgb_eotf(g), _srgb_eotf(b)
    l_ = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m_ = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s_ = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    C = math.hypot(a, bb)
    h = math.degrees(math.atan2(bb, a)) % 360 if C > 1e-6 else 0.0
    return L, C, h


def _rgba(css_value: str) -> tuple[float, float, float, float]:
    """(r, g, b, a) in 0–1 from a hex or oklch() string."""
    v = css_value.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
        return r, g, b, int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
    m = _OKLCH.match(v)
    if not m:
        raise ValueError(f"unsupported colour syntax: {v}")
    L = float(m.group(1)) / (100 if m.group(2) else 1)
    r, g, b = _oklch_to_srgb(L, float(m.group(3)), float(m.group(4)))
    alpha = float(m.group(5)) / (100 if m.group(6) else 1) if m.group(5) else 1.0
    return r, g, b, alpha


def _oklch(L: float, C: float, h: float, alpha: float = 1.0) -> str:
    L = min(max(L, 0.0), 1.0)
    tail = "" if alpha >= 1.0 else f" / {alpha:g}"
    return f"oklch({L * 100:.1f}% {C:.3f} {h:.1f}{tail})"


def _shift(
    css_value: str, *, L: float | None = None, dL: float = 0.0, C: float | None = None
) -> str:
    """A colour with its OKLCH lightness moved (``L`` sets it, ``dL`` adds to it) and chroma scaled."""
    r, g, b, _ = _rgba(css_value)
    L0, C0, h = _srgb_to_oklch(r, g, b)
    return _oklch(
        L if L is not None else L0 + dL, C0 * (C if C is not None else 1.0), h
    )


def _alpha(css_value: str, alpha: float) -> str:
    r, g, b, _ = _rgba(css_value)
    L, C, h = _srgb_to_oklch(r, g, b)
    return _oklch(L, C, h, alpha)


def _derive(p: Palette) -> dict[str, str]:
    """Every theme-dependent token from a palette. Fixed tokens are added by :func:`color_tokens`."""
    bg_L = _srgb_to_oklch(*_rgba(p.bg)[:3])[0]
    ground_L = max(bg_L + 0.09, 0.22)
    return {
        "scene-bg": p.bg,
        "scene-ground": _shift(p.bg, L=ground_L),
        "scene-grid": _shift(p.bg, L=ground_L + 0.16),
        "surface": p.surface,
        "glass": _alpha(p.surface, 0.86),
        "glass-end": _alpha(_shift(p.surface, dL=-0.08), 0.86),
        "glass-border": _alpha(p.border, 0.5),
        "well": _alpha(p.bg, 0.55),
        "state-hover": _alpha(p.text, 0.08),
        "state-selected": _alpha(p.text, 0.16),
        "text": p.text,
        "text-muted": p.muted,
        "text-disabled": _shift(
            p.muted, L=(bg_L + _srgb_to_oklch(*_rgba(p.muted)[:3])[0]) / 2
        ),
        "action": p.accent,
        "action-hover": _shift(p.accent, dL=-0.1),
        "action-text": _shift(p.accent, L=0.88, C=0.5),
        "focus-ring": _shift(p.accent, L=0.75),
        "control": p.surface_2,
        "progress": _shift(p.accent, L=0.75),
        "positive": _shift(p.green, L=0.88, C=0.6),
        "positive-soft": _alpha(p.green, 0.12),
        "warning": _shift(p.yellow, L=0.92, C=0.7),
        "warning-fill": _shift(p.yellow, L=0.85),
        "warning-soft": _alpha(p.yellow, 0.1),
        "error": _shift(p.red, L=0.86, C=0.5),
        "error-soft": _alpha(p.red, 0.08),
        "info": _shift(p.accent, L=0.88, C=0.5),
        "run": _shift(p.green, L=0.7),
        "fill-positive": _shift(p.green, L=0.5),
        "fill-warning": _shift(p.orange, L=0.55),
        "fill-error": _shift(p.red, L=0.5),
        "fill-info": _shift(p.accent, L=0.5),
        "ai-inspect": _shift(p.green, L=0.76),
        "ai-inspect-text": _shift(p.green, L=0.84),
        "ai-auto-edits": _shift(p.blue, L=0.75),
        "ai-auto-edits-text": _shift(p.blue, L=0.9, C=0.5),
        "ai-autopilot": _shift(p.magenta, L=0.7),
        "ai-autopilot-text": _shift(p.magenta, L=0.89, C=0.5),
        "measure-position": _shift(p.cyan, L=0.78),
        "measure-current": _shift(p.magenta, L=0.83),
        "path-cartesian": p.green,
        "path-joints": p.blue,
        "path-smooth": p.magenta,
        "path-invalid": p.red,
        "path-timing-warning": _shift(p.yellow, L=0.85),
        "path-checkpoint": p.muted,
        "path-tool-action": FIXED_COLOR["scene-tool"],
        "physics-on-track": _shift(p.green, L=0.8),
        "physics-diverged": _shift(p.red, L=0.65),
        "physics-contact": _shift(p.red, L=0.7),
        "physics-com": _shift(p.text, L=0.92),
        "chart-1": p.blue,
        "chart-2": p.green,
        "chart-3": p.orange,
        "chart-4": p.red,
        "chart-5": p.magenta,
        "chart-6": p.yellow,
    }


@lru_cache(maxsize=None)
def color_tokens(theme: str) -> dict[str, str]:
    """name -> CSS colour for a theme, fixed tokens included."""
    return {**_derive(THEMES[theme]), **FIXED_COLOR}


@lru_cache(maxsize=None)
def _hex_table(theme: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for name, value in color_tokens(theme).items():
        r, g, b, a = _rgba(value)
        if a >= 1.0:
            table[name] = "#" + "".join(f"{round(x * 255):02x}" for x in (r, g, b))
    return table


_active_theme: str = DEFAULT_THEME


def theme_names() -> list[str]:
    return list(THEMES)


def stored_theme() -> str:
    """The theme the user chose, or the default."""
    try:
        name = app.storage.general.get(STORAGE_KEY)
    except RuntimeError:
        name = None
    return name if name in THEMES else DEFAULT_THEME


# ── Token adapters ───────────────────────────────────────────────────


def css(name: str) -> str:
    """CSS value for a token, for classes and inline styles."""
    return f"var(--wc-{name})"


def quasar(name: str) -> str:
    """Quasar colour name for a token, for ``color=`` and ``text-color=`` props."""
    return f"wc-{name}"


def hex_of(name: str) -> str:
    """Opaque hex for a token in the active theme, for Three.js and charts.

    Translucent tokens (glass, well, soft tints, scrim, hover states) have no
    single hex and raise ``KeyError``.
    """
    return _hex_table(_active_theme)[name]


def linear_rgb(name: str) -> list[float]:
    """A token as a linear-light RGB triple in 0–1, for Three.js vertex colours."""
    return [_srgb_eotf(c) for c in _rgba(hex_of(name))[:3]]


def effective_theme() -> ThemeKey:
    """Whether the active theme is dark or light, for CodeMirror and the like."""
    return "dark"


class SceneColors:
    """Hex constants for the fixed scene colours: code and tests that need a literal.

    Theme-dependent surfaces (background, ground, grid, arm) go through
    :func:`hex_of` at the point of use.
    """

    AXIS_X_HEX = FIXED_COLOR["axis-x"]
    AXIS_Y_HEX = FIXED_COLOR["axis-y"]
    AXIS_Z_HEX = FIXED_COLOR["axis-z"]
    AXIS_RX_HEX = FIXED_COLOR["axis-rx"]
    AXIS_RY_HEX = FIXED_COLOR["axis-ry"]
    AXIS_RZ_HEX = FIXED_COLOR["axis-rz"]

    SIM_AMBER_HEX = FIXED_COLOR["scene-arm-sim"]
    EDIT_GRAY_HEX = FIXED_COLOR["scene-arm-edit"]
    COLLISION_HEX = FIXED_COLOR["scene-collision"]

    SHAPE_HEX = FIXED_COLOR["scene-shape"]
    SHAPE_DRAFT_HEX = FIXED_COLOR["scene-shape-draft"]
    SHAPE_INSTALL_HEX = FIXED_COLOR["scene-shape-install"]
    SHAPE_PROPOSED_HEX = FIXED_COLOR["scene-shape-proposed"]

    TOOL_BODY_HEX = FIXED_COLOR["scene-tool"]
    TOOL_BODY_SIM_HEX = TOOL_BODY_HEX
    TOOL_BODY_EDIT_HEX = FIXED_COLOR["scene-tool-edit"]
    TOOL_MOVING_HEX = FIXED_COLOR["scene-tool-moving"]
    TOOL_MOVING_SIM_HEX = TOOL_MOVING_HEX
    TOOL_MOVING_EDIT_HEX = FIXED_COLOR["scene-tool-moving-edit"]

    ENVELOPE_HEX = AXIS_Z_HEX
    TCP_ACTIVE_HEX = AXIS_Z_HEX


_MOVE_TYPE_TOKENS: dict[str, str] = {
    "cartesian": "path-cartesian",
    "joints": "path-joints",
    "smooth": "path-smooth",
    "smooth_arc": "path-smooth",
    "smooth_spline": "path-smooth",
    "jog": "path-cartesian",
    "unknown": "path-cartesian",
}


def get_color_for_move_type(
    move_type: str, is_valid: bool = True, timing_feasible: bool = True
) -> str:
    """Segment colour for a move type in the active theme.

    An unreachable segment wins over one that only overruns its requested
    duration.
    """
    if not is_valid:
        return hex_of("path-invalid")
    if not timing_feasible:
        return hex_of("path-timing-warning")

    key = move_type.lower() if move_type else "unknown"
    if key not in _MOVE_TYPE_TOKENS:
        if "smooth" in key:
            key = "smooth"
        else:
            key = "unknown"
    return hex_of(_MOVE_TYPE_TOKENS[key])


def _token_block(theme: str) -> str:
    lines = [f"  --wc-{n}: {v};" for n, v in color_tokens(theme).items()]
    lines.append(f"  --wc-shadow-glass: {SHADOW['shadow-glass']};")
    return "\n".join(lines)


def _scalar_block() -> str:
    lines: list[str] = []
    for group in (SPACE, RADIUS, SIZE, EFFECT, OPACITY, Z_INDEX, DURATION, EASING):
        lines.extend(f"  --wc-{n}: {v};" for n, v in group.items())
    lines.extend(f"  --wc-font-{n}: {v};" for n, v in FONT_FAMILY.items())
    lines.append(
        "  --wc-footer-clearance: calc(var(--wc-size-footer) + 2 * var(--wc-space-3));"
    )
    # The footer's cover until PanelResize measures what the column stops above.
    lines.append(
        "  --wc-column-cover: calc(var(--wc-size-footer) + var(--wc-space-3));"
    )
    return "\n".join(lines)


def _type_classes() -> str:
    rules = []
    for name, (size, lh, weight, spacing) in TYPE_STYLE.items():
        extra = f" letter-spacing: {spacing};" if spacing else ""
        rules.append(
            f".wc-{name} {{ font: {weight} {size}/{lh} var(--wc-font-sans);{extra} }}"
        )
    return "\n".join(rules)


def _inject_tokens_css() -> None:
    """Emit every token of the active theme as ``--wc-*`` on ``:root``."""
    ui.add_css(
        f"""
:root {{
{_token_block(_active_theme)}
{_scalar_block()}
}}

{_type_classes()}
"""
    )


_GLASS_SELECTORS = (
    ".glass, .overlay-card, .side-tab-bar, .ai-cluster, .ai-approval-card,"
    " .tutorial-dialog-card, .bottom-playback-bar, .q-dialog__inner > .q-card, .q-menu,"
    " .status-footer, .bottom-panel"
)


def _inject_component_overrides() -> None:
    """Quasar and app component styling on top of the tokens."""
    ui.add_css(
        f"""
body {{ color: var(--wc-text); font-family: var(--wc-font-sans); }}
body.body--dark, body.body--light, .q-page {{ background: transparent !important; }}
.q-layout, .q-page-container {{ background: transparent !important; }}

/* ========== Surfaces ========== */

{_GLASS_SELECTORS} {{
  background: linear-gradient(135deg, var(--wc-glass), var(--wc-glass-end)) !important;
  backdrop-filter: blur(var(--wc-glass-blur)) saturate(var(--wc-glass-saturate));
  -webkit-backdrop-filter: blur(var(--wc-glass-blur)) saturate(var(--wc-glass-saturate));
  border: 1px solid var(--wc-glass-border) !important;
  border-radius: var(--wc-radius-md);
  box-shadow: var(--wc-shadow-glass);
  color: var(--wc-text);
  isolation: isolate;
}}
.well {{ background: var(--wc-well); border-radius: var(--wc-radius-sm); }}
.q-item, .q-toolbar, .q-field, .q-tab-panels, .q-tab-panel {{ background: transparent; }}
.q-separator {{ background: var(--wc-glass-border); }}

/* Text on a fill is set per element with text-color=: Quasar's white default
   sits in a CSS layer that outranks app rules, so no CSS pairing is attempted. */
.q-btn.bg-wc-action:hover {{ background: var(--wc-action-hover) !important; }}

/* ========== Buttons ========== */

.q-btn {{ text-transform: none; }}
.q-btn:not(.q-btn--round) {{
  border-radius: var(--wc-radius-sm);
  padding: 3px 6px !important;
  min-height: var(--wc-size-control) !important;
  min-width: var(--wc-size-control) !important;
}}
.q-btn--flat, .q-btn--outline {{ color: var(--wc-text); }}
.q-btn:focus-visible, .q-field__native:focus-visible, .q-tab:focus-visible {{
  outline: 2px solid var(--wc-focus-ring);
  outline-offset: 2px;
}}
.q-slider__thumb {{ width: 30px !important; height: 30px !important; }}
.q-slider__track {{ height: 8px !important; }}

/* Segmented toggle */
.q-btn-toggle .q-btn {{ border-radius: var(--wc-radius-sm); }}
.q-btn-toggle .q-btn.q-btn--active {{ background: var(--wc-action); color: var(--wc-on-bright); }}
.q-btn-toggle .q-btn:not(.q-btn--active) {{ background: var(--wc-control); color: var(--wc-text); }}

/* Tabs: sentence case, action-text indicator */
.q-tab {{ text-transform: none; }}
.q-tab__indicator {{ background: var(--wc-action-text) !important; }}

/* ========== Inputs ========== */

.q-field__native, .q-field__input, .q-field__prefix, .q-field__suffix {{ color: var(--wc-text); }}
.q-field:not(.q-field--highlighted) .q-field__label {{ color: var(--wc-text-muted); }}
.q-field:not(.q-field--borderless) .q-field__control {{
  background: var(--wc-well);
  border-radius: var(--wc-radius-sm);
}}
.q-field--error .q-field__bottom, .q-field--error .q-field__messages {{ color: var(--wc-error) !important; }}
.step-input .q-field__suffix {{ display: inline-block; width: 14px; text-align: center; }}
.step-suffix-small .q-field__suffix {{ font-size: 0.7em; }}

/* Disable input steppers for numerical input */
input::-webkit-outer-spin-button,
input::-webkit-inner-spin-button {{
  -webkit-appearance: none;
  margin: 0;
}}
input[type=number] {{
  -moz-appearance: textfield;
}}

/* Locked out: the AI drives, or another tab owns the session */
.cp-disabled-strong {{
  opacity: var(--wc-opacity-locked) !important;
  filter: grayscale(1) contrast(0.6) brightness(0.8);
  pointer-events: none !important;
  cursor: not-allowed !important;
  box-shadow: none !important;
}}
"""
    )


def apply_theme() -> None:
    """Put the stored theme on the page: Quasar colours, dark mode and the token CSS."""
    global _active_theme
    _active_theme = stored_theme()
    ui.colors(
        primary=css("action"),
        secondary=css("action-hover"),
        accent=css("action-text"),
        dark=css("glass-end"),
        dark_page=css("scene-bg"),
        positive=css("fill-positive"),
        negative=css("fill-error"),
        info=css("fill-info"),
        warning=css("fill-warning"),
        **{quasar(n): css(n) for n in color_tokens(_active_theme)},
    )
    ui.dark_mode().enable()
    _inject_tokens_css()
    _inject_component_overrides()


def _px(value: str) -> int:
    return int(value.removesuffix("px"))


# The --wc-footer-clearance the panels keep from the bottom edge, in px.
_FOOTER_CLEARANCE = _px(SIZE["size-footer"]) + 2 * _px(SPACE["space-3"])

# Panel resize configuration (passed to JS module)
PANEL_RESIZE_CONFIG: dict[str, Any] = {
    "storageKey": "parol_panel_sizes",
    "selectors": {
        "topContainer": ".top-panels-container",
        "bottomContainer": ".bottom-panels-container",
        "controlPanel": ".overlay-br",
        "bottomCovers": [".status-footer", ".bottom-panel"],
        "columnCovers": [".bottom-panels-container"],
    },
    "constraints": {
        "viewportMarginX": 80,
        # The top margin and the footer clearance.
        "viewportMarginY": _FOOTER_CLEARANCE + _px(SPACE["space-3"]),
        # The same, plus the gap between two coupled panels.
        "totalMargin": _FOOTER_CLEARANCE + 2 * _px(SPACE["space-3"]),
    },
    "panels": {
        "program": {
            "selector": ".top-panels-container .program-panel",
            "minWidth": 450,
            "defaultWidth": 680,
            "fullHeight": True,
            "group": "top",
        },
        "gripper": {
            "selector": ".top-panels-container .gripper-panel",
            "minWidth": 378,
            "minHeight": 310,
            "defaultWidth": 378,
            "defaultHeight": 310,
            "cameraWidth": 660,
            "cameraHeight": 675,
            "group": "top",
        },
    },
}


def _generate_resize_handle_css() -> str:
    """Generate resize handle CSS for all panel positions."""
    specs = {
        "side": {"size": "12px", "indicator": "4px", "len": "50px"},
        "corner": {"size": "16px", "indicator": "8px"},
    }

    containers = {
        ".top-panels-container": ["right", "bottom", "corner"],
        ".bottom-panels-container": ["right", "top", "corner"],
    }
    fast = "var(--wc-duration-fast) var(--wc-ease-enter)"

    css_parts = []
    for container, handles in containers.items():
        for name in handles:
            if name == "corner":
                s, i = specs["corner"]["size"], specs["corner"]["indicator"]
                # Vertical anchor flips with container type (top vs bottom).
                v_pos = "bottom" if "top" in container else "top"
                cursor = "nwse-resize" if "top" in container else "nesw-resize"

                css_parts.append(
                    f"""
{container} .resizable-panel .resize-handle-{name} {{
  right: -4px; {v_pos}: -4px;
  width: {s}; height: {s};
  cursor: {cursor};
  z-index: 101;
}}
{container} .resizable-panel .resize-handle-{name}::after {{
  width: {i}; height: {i};
  transition: background {fast}, width {fast}, height {fast};
}}"""
                )
            else:
                # Orientation inferred from the handle name (left/right are vertical).
                is_vert = name in ("left", "right")
                dim_prop, len_prop = (
                    ("width", "height") if is_vert else ("height", "width")
                )
                pos_spread = "top: 0; bottom: 0" if is_vert else "left: 0; right: 0"
                cursor = "ew-resize" if is_vert else "ns-resize"

                s = specs["side"]["size"]
                i_thick, i_len = specs["side"]["indicator"], specs["side"]["len"]

                css_parts.append(
                    f"""
{container} .resizable-panel .resize-handle-{name} {{
  {name}: -4px; {pos_spread};
  {dim_prop}: {s};
  cursor: {cursor};
}}
{container} .resizable-panel .resize-handle-{name}::after {{
  {dim_prop}: {i_thick}; {len_prop}: {i_len};
  transition: background {fast}, width {fast}, height {fast};
}}
{container} .resizable-panel .resize-handle-{name}:hover::after {{
  {len_prop}: 70px;
}}
{container} .resizable-panel .resize-handle-{name}.dragging::after {{
  {len_prop}: 90px;
}}"""
                )

    return "\n".join(css_parts)


_RESIZE_HANDLE_CSS = _generate_resize_handle_css()


def inject_layout_css() -> None:
    """Injects the app's layout and component CSS previously embedded in main.py."""
    from waldo_commander.common.panel_theme import inject_panel_css

    inject_panel_css()
    ui.add_css(
        """
/* Prevent full-page scrollbar flash globally */
html, body {
  overflow: hidden !important;
  height: 100%;
  width: 100%;
}

.q-page { overflow: hidden !important; }

/* Main app container should also clip */
.q-layout, .q-page-container { overflow: hidden !important; }

/* Joint readout: a compact number field centred in its dial */
.joint-readout-input .q-field__control {
  max-height: 3em !important;
  padding: 0 !important;
}
.joint-readout-input .q-field__native {
  padding: 0 !important;
  font-size: 10px;
  line-height: 1;
}
.joint-readout-input .q-field__suffix {
  font-size: 9px;
  padding-left: 0;
}

/* Axis colours: fills (glyphs, markers) and their text variants (readout) */
.tcp-x  { color: var(--wc-axis-x); }
.tcp-rx { color: var(--wc-axis-rx); }
.tcp-y  { color: var(--wc-axis-y); }
.tcp-ry { color: var(--wc-axis-ry); }
.tcp-z  { color: var(--wc-axis-z); }
.tcp-rz { color: var(--wc-axis-rz); }
.tcp-x-text  { color: var(--wc-axis-x-text); }
.tcp-rx-text { color: var(--wc-axis-rx-text); }
.tcp-y-text  { color: var(--wc-axis-y-text); }
.tcp-ry-text { color: var(--wc-axis-ry-text); }
.tcp-z-text  { color: var(--wc-axis-z-text); }
.tcp-rz-text { color: var(--wc-axis-rz-text); }


/* ========== Controls ========== */

/* Cartesian jog buttons: a fixed-size hit box hosting a currentColor SVG
   glyph with the axis label overlaid as HTML (assets carry no text). */
.cart-jog-slot {
  position: relative;
  width: var(--wc-size-jog-slot);
  height: var(--wc-size-jog-slot);
  cursor: pointer;
}

.cart-jog-slot .cart-jog-glyph {
  position: absolute;
  inset: 0;
}

.cart-jog-slot svg {
  width: 100%;
  height: 100%;
  display: block;
  fill: currentColor;
  stroke: currentColor;
}

/* Anchored per slot via inline left/top/font-size: the label's left edge
   sits at the glyph's original in-SVG text anchor, vertically centered on
   that line. The text stroke reproduces the chunky stroked look the labels
   had as SVG <text stroke="#000">. */
.cart-jog-label {
  position: absolute;
  transform: translateY(-50%);
  line-height: 1;
  white-space: nowrap;
  font-weight: 800;
  letter-spacing: 0.15em;
  color: var(--wc-on-bright);
  -webkit-text-stroke: 0.09em var(--wc-on-bright);
  pointer-events: none;
  user-select: none;
}

/* Pressed visual feedback for jog controls */
.is-pressed {
  transform: scale(0.96);
  filter: brightness(1.2);
  outline: 1px solid var(--wc-focus-ring);
  transition: transform var(--wc-duration-instant) linear, filter var(--wc-duration-instant) linear, outline-color var(--wc-duration-instant) linear;
}

/* Joint dials: a ring per joint on a control track with the travelled arc in
   progress; the jog caps and limit buttons appear when the dial is hovered or
   holds focus */
.joint-dial-cell {
  width: var(--wc-size-joint-dial);
  position: relative;
}
.joint-dial {
  position: relative;
  width: var(--wc-size-joint-dial);
  height: var(--wc-size-joint-dial);
}
.joint-dial-svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
}
.dial-track, .dial-fill {
  fill: none;
  stroke-width: 5;
  stroke-linecap: round;
}
.dial-track { stroke: var(--wc-control); }
.dial-fill { stroke: var(--wc-progress); }
.dial-tick { stroke: var(--wc-text-muted); stroke-width: 1.5; }
.dial-knob { fill: var(--wc-text); }
.joint-dial .joint-readout-input {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 44px;
  color: var(--wc-text);
  font-variant-numeric: tabular-nums;
}
.joint-cap {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 24px;
  height: 24px;
  min-height: 0;
  padding: 0;
  color: var(--wc-text) !important;
  background: var(--wc-glass);
  opacity: 0;
  transition: opacity var(--wc-duration-fast);
  z-index: 1;
}
.joint-cap-minus { left: -6px; }
.joint-cap-plus { right: -6px; }
.joint-cap.q-btn--disabled {
  color: var(--wc-text-disabled) !important;
  pointer-events: none;
}
.joint-dial-name {
  max-width: 100%;
  font-size: 10px;
  line-height: 14px;
  color: var(--wc-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.joint-dial-limits {
  opacity: 0;
  transition: opacity var(--wc-duration-fast);
}
.joint-dial-cell:hover .joint-cap,
.joint-dial-cell:focus-within .joint-cap,
.joint-dial-cell:hover .joint-dial-limits,
.joint-dial-cell:focus-within .joint-dial-limits {
  opacity: 1;
}
/* Visibility, not opacity: Quasar's disabled opacity is !important inside a cascade
   layer, which outranks any unlayered rule, so a cap at its limit would show */
.joint-dial-cell:not(:hover):not(:focus-within) .joint-cap,
.joint-dial-cell:not(:hover):not(:focus-within) .joint-dial-limits { visibility: hidden; }

/* Level chips: percentage beside the icon, the rating in the popover */
.level-chip { font-variant-numeric: tabular-nums; }
.level-chip .q-icon { color: var(--wc-text-muted); }
.level-menu { padding: var(--wc-space-1) var(--wc-space-2); }

/* Tool box: name over its readout, the actions to the right; the readout is a
   bare card so its updates stay inside it */
.tool-box-readout { min-width: 0; line-height: 1.1; background: transparent; color: inherit; }

/* Control panel jog tabs: compact padding */
.cp-jog-tabs .q-tab {
  padding: 0 14px !important;
  min-height: 28px !important;
}
.cp-jog-panels .q-tab-panels,
.cp-jog-panels .q-tab-panel {
  padding: 0 !important;
  overflow: hidden;
}

/* Record button: a control with a record dot, filled with the record colour
   while recording, when the dot takes the text colour and pulses */
.record-btn:not(.recording) .q-icon { color: var(--wc-record); }
.record-btn.recording .q-icon { animation: recording-pulse var(--wc-duration-ambient) var(--wc-ease-loop) infinite; }


/* ========== Overlays ========== */

/* AI-driving perimeter glow: breathes while an AI session holds the lease */
@keyframes wc-glow-breathe {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}
.control-glow-breathe { animation: wc-glow-breathe var(--wc-duration-ambient) var(--wc-ease-loop) infinite; }

/* ---- AI control cluster ----
   One --mode-accent per control mode themes the perimeter glow and the
   top-center capsule. The accents live only here. */
.wc-mode-inspect    { --mode-accent: var(--wc-ai-inspect);    --mode-accent-text: var(--wc-ai-inspect-text); }
.wc-mode-auto-edits { --mode-accent: var(--wc-ai-auto-edits); --mode-accent-text: var(--wc-ai-auto-edits-text); }
.wc-mode-autopilot  { --mode-accent: var(--wc-ai-autopilot);  --mode-accent-text: var(--wc-ai-autopilot-text); }

/* CSS-variable scope over glow + capsule; generates no box, so the fixed
   children still position against the viewport. */
.ai-mode-scope { display: contents; }

.control-lease-glow {
  position: fixed; inset: 0; pointer-events: none; z-index: var(--wc-z-glow);
  box-shadow: inset 0 0 18px 2px color-mix(in srgb, var(--mode-accent) 30%, transparent),
              inset 0 0 60px 8px color-mix(in srgb, var(--mode-accent) 12%, transparent);
}
/* An MCP client is connected but the human drives. */
.control-lease-glow.glow-faint { opacity: 0.35; }

/* Glass capsule holding the mode chip + Take-control button. Its own
   backdrop-filter makes it a containing block — fine while it has no
   position:fixed descendants (the glow is a sibling). */
.ai-cluster {
  position: fixed; top: 8px; left: 50%; transform: translateX(-50%); z-index: var(--wc-z-capsule);
  display: flex; align-items: center; gap: var(--wc-space-1); padding: 3px 4px;
  border-radius: var(--wc-radius-pill);
  border-color: color-mix(in srgb, var(--mode-accent) 35%, transparent) !important;
  transition: border-color var(--wc-duration-base) var(--wc-ease-enter);
}
.ai-cluster.ai-driving { border-color: color-mix(in srgb, var(--mode-accent) 65%, transparent) !important; }

/* Text-only at rest so the capsule reads as one pill (no pill-in-pill);
   the hover tint is the click affordance. */
.ai-cluster .control-mode-chip {
  background: transparent !important;
  color: var(--mode-accent-text) !important;
  border-radius: var(--wc-radius-pill); font-weight: 500; margin: 0;
}
.ai-cluster .control-mode-chip:hover {
  background: color-mix(in srgb, var(--mode-accent) 15%, transparent) !important;
}

/* The only solid-filled element in the capsule: pops out when the AI takes
   the lease (the entry animation replays on every hidden -> visible flip)
   and pulses on the glow-breathe clock. */
.ai-cluster .btn-take-control {
  background: var(--mode-accent) !important;
  color: var(--wc-on-bright) !important;
  border-radius: var(--wc-radius-pill); font-weight: 600;
  /* Chip-height so the capsule doesn't grow when the button pops in. */
  font-size: 0.75rem; min-height: 0; padding: 1px 10px;
  animation: wc-popout var(--wc-duration-base) var(--wc-ease-pop),
             wc-btn-pulse var(--wc-duration-ambient) var(--wc-ease-loop) infinite;
}
.ai-cluster .btn-take-control .q-icon { font-size: 1.3em; }
@keyframes wc-popout {
  from { transform: translateX(-10px) scale(0.85); opacity: 0; }
  to   { transform: none; opacity: 1; }
}
@keyframes wc-btn-pulse {
  0%, 100% { box-shadow: 0 0 10px 2px color-mix(in srgb, var(--mode-accent) 55%, transparent); }
  50%      { box-shadow: 0 0 2px 0   color-mix(in srgb, var(--mode-accent) 20%, transparent); }
}

/* Approval dialog: a standard glass panel. Allow is the action button; the
   hardware-consent variant is the caution treatment. */
.ai-approval-card {
  min-width: 320px; max-width: 440px;
}
.ai-approval-card .ai-approval-desc {
  background: var(--wc-well);
  border-left: 2px solid var(--wc-action);
  border-radius: var(--wc-radius-sm); padding: 6px 10px;
}
.ai-approval-card .btn-consent-allow {
  background: var(--wc-action) !important;
  color: var(--wc-on-bright) !important;
}
.ai-approval-card .btn-consent-allow:hover {
  background: var(--wc-action-hover) !important;
}
.ai-approval-card.consent-hw .ai-approval-icon { color: var(--wc-warning); }
.ai-approval-card.consent-hw .ai-approval-desc { border-left-color: var(--wc-warning-fill); }
.ai-approval-card.consent-hw .btn-consent-allow {
  background: var(--wc-warning-fill) !important;
  color: var(--wc-on-bright) !important;
}

/* Overlay panels with frosted glass effect */
.overlay-panel { position: absolute; z-index: var(--wc-z-cards); pointer-events: auto; }
.overlay-card {
  padding: var(--wc-space-3);
}

.overlay-br { bottom: var(--wc-footer-clearance); right: var(--wc-space-3); }


/* ========== Left Tabs ========== */

/* Reduce vertical tab padding to match right side panel margins (12px) */
.q-tabs--vertical .q-tab {
  padding: 8px 12px !important;
  min-height: 44px !important;
}

/* Make left tab column narrower */
.q-tabs--vertical {
  width: var(--wc-size-rail) !important;
}

/* Side tab bar: glass, unified bar appearance */
.side-tab-bar {
  margin: var(--wc-space-3);
  padding: var(--wc-space-1) 0;
  pointer-events: auto;
  height: auto !important;
  min-height: 0 !important;
  z-index: var(--wc-z-rail);
}
.side-tab-bar.absolute.bottom-0 { z-index: var(--wc-z-rail-bottom); margin-bottom: var(--wc-footer-clearance); }
/* The bottom rail is the gear alone unless a plugin adds a tab above it. */
.bottom-rail { display: flex; flex-direction: column; align-items: stretch; width: var(--wc-size-rail); }
.bottom-rail .q-tabs--vertical { width: 100% !important; }
.bottom-rail .rail-gear {
  width: 100%;
  min-height: 44px !important;
  border-radius: 0 !important;
  padding: 8px 12px !important;
}

/* Ensure tabs inside the bar have proper sizing */
.side-tab-bar .q-tab {
  min-height: 44px !important;
  padding: 8px 12px !important;
}

/* Tab flash: content landed in a panel that is not on screen. */
@keyframes tab-flash {
  0%, 50% { background-color: color-mix(in srgb, var(--wc-positive) 40%, transparent); }
  25%, 75% { background-color: transparent; }
  100% { background-color: transparent; }
}
.tab-flash {
  animation: tab-flash var(--wc-duration-flash) var(--wc-ease-enter) 1;
}

/* Shared left-side panel container base styling */
.left-panels-container {
  position: absolute;
  z-index: var(--wc-z-panels);
  left: var(--wc-size-panel-inset);
  max-width: calc(100vw - 80px);
  overflow: hidden !important;
  scrollbar-width: none !important;
  -ms-overflow-style: none !important;
}

.left-panels-container::-webkit-scrollbar { display: none !important; }

.top-panels-container { top: var(--wc-space-3); }

.bottom-panels-container { bottom: var(--wc-footer-clearance); }

.resizable-panel { overflow: hidden !important; }

/* A panel whose container carries no inline height (no dragged size yet) is as
   tall as its content. This keeps it inside the viewport; the flex chain under
   it (min-height: 0, overflow: auto) scrolls at the cap. */
.left-panels-container > .q-panel > .resizable-panel { max-height: calc(100vh - 24px); }
.bottom-panels-container > .q-panel > .resizable-panel {
  max-height: calc(100vh - var(--wc-space-3) - var(--wc-footer-clearance));
}

/* The program column: full height between the top margin and whatever covers
   the bottom (the footer, the bottom panel as a terminal sits under an
   editor, a bottom plugin panel), width from PanelResize; only the right edge
   is a handle. */
.panels-wrap.column-open .top-panels-container {
  top: var(--wc-space-3);
  bottom: calc(var(--wc-column-cover) + var(--wc-space-3));
  height: auto !important;
  min-height: var(--wc-size-column-min);
}
.panels-wrap.column-open .top-panels-container > .q-panel > .program-panel { max-height: none; }

/* Panel content is interactive when visible */
.left-panels-container .overlay-card { pointer-events: auto; }

/* Tab panels appear to come from underneath with left edge shadow */
.top-panels-container .q-tab-panel.overlay-card {
  border-top-left-radius: 0 !important;
  border-bottom-left-radius: var(--wc-radius-md) !important;
  box-shadow:
    inset 4px 0 8px -4px var(--wc-scrim),
    var(--wc-shadow-glass);
}

/* Bottom panels appear to come from underneath the tab bar */
.bottom-panels-container .q-tab-panel.overlay-card {
  border-bottom-left-radius: 0 !important;
}

/* Custom slide animations for left panels - override Quasar transitions */
/* These ensure panels always slide in from left and out to left */
@keyframes panel-slide-enter {
  from {
    transform: translateX(-100%);
    opacity: 0;
  }
  to {
    transform: translateX(0);
    opacity: 1;
  }
}

@keyframes panel-slide-leave {
  from {
    transform: translateX(0);
    opacity: 1;
  }
  to {
    transform: translateX(-100%);
    opacity: 0;
  }
}

/* Apply custom animations to left panel transitions - target .q-panel.scroll */
.left-panels-container .q-panel.scroll[class*="q-transition--slide"] {
  animation-duration: var(--wc-duration-base) !important;
  animation-timing-function: var(--wc-ease-enter) !important;
}

/* Entering panel - slide in from left */
.left-panels-container .q-panel.scroll.q-transition--slide-right-enter-active,
.left-panels-container .q-panel.scroll.q-transition--slide-left-enter-active {
  animation-name: panel-slide-enter !important;
}

/* Leaving panel - slide out to left */
.left-panels-container .q-panel.scroll.q-transition--slide-right-leave-active,
.left-panels-container .q-panel.scroll.q-transition--slide-left-leave-active {
  animation-name: panel-slide-leave !important;
}

/* Also handle vertical transitions (slide-up/slide-down) that Quasar uses for first tab open */
.left-panels-container .q-panel.scroll.q-transition--slide-up-enter-active,
.left-panels-container .q-panel.scroll.q-transition--slide-down-enter-active {
  animation-name: panel-slide-enter !important;
}

.left-panels-container .q-panel.scroll.q-transition--slide-up-leave-active,
.left-panels-container .q-panel.scroll.q-transition--slide-down-leave-active {
  animation-name: panel-slide-leave !important;
}

/* Log line coloring for response log */
.nicegui-log .log-trace   { color: var(--wc-info); }
.nicegui-log .log-debug   { color: var(--wc-text-muted); }
.nicegui-log .log-info    { color: var(--wc-text); }
.nicegui-log .log-warning { color: var(--wc-warning); }
.nicegui-log .log-error   { color: var(--wc-error); }
.nicegui-log .log-critical {
  color: var(--wc-error);
  background: var(--wc-error-soft);
  padding: 0 4px;
  border-radius: 3px;
}

/* ========== Resize Handles ========== */

/* Base resize handle styles (shared by all resizable panels) */
[class*="resize-handle-"] {
  position: absolute;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
}

[class*="resize-handle-"]::after {
  content: '';
  background: var(--wc-state-selected);
  border-radius: 2px;
}

[class*="resize-handle-"]:hover::after,
[class*="resize-handle-"].dragging::after { background: var(--wc-text-muted); }

/* ========== Editor Tabs ========== */

/* The program panel is a folder: the header row is its top edge, and the active
   tab rises out of it, joined to the editor outline below (the header overlaps
   the editor's top border by 1px and the tab's glass covers the seam). */
.editor-header { position: relative; z-index: 1; margin-bottom: -1px; }
.editor-tabs .q-tab {
  padding: 4px 8px !important;
  min-height: 42px !important;
}
.editor-tabs .q-tab__indicator { display: none; }

.editor-tab {
  border: 1px solid transparent;
  border-bottom: 0;
  border-radius: var(--wc-radius-sm) var(--wc-radius-sm) 0 0;
  margin-right: 2px;
  transition: background var(--wc-duration-fast) var(--wc-ease-enter);
}

.editor-tab:hover { background: var(--wc-state-hover); }

.editor-tab.q-tab--active {
  background: var(--wc-glass);
  border-color: var(--wc-glass-border);
}
/* TODO: only half works */
/* Disable pointer events on active tab (prevent re-clicking), but allow input and close button */
.editor-tab.q-tab--active,
.editor-tab.q-tab--active .q-tab__content,
.editor-tab.q-tab--active .q-focus-helper,
.editor-tab.q-tab--active .q-tab__indicator {
  pointer-events: none !important;
}
.editor-tab.q-tab--active .q-field,
.editor-tab.q-tab--active .q-btn { pointer-events: auto !important; }

/* Compact filename input in tabs */
.editor-tab .q-field { min-height: var(--wc-size-control-sm) !important; }

.editor-tab .q-field__control {
  height: var(--wc-size-control-sm) !important;
  min-height: var(--wc-size-control-sm) !important;
}

.editor-tab .q-field__native {
  padding: 0 4px !important;
  min-height: 20px !important;
  font-size: 0.85rem;
}

/* Editor tabs scroll area - no padding */
.editor-tabs-scroll .q-scrollarea__content {
  padding: 0 !important;
  gap: 0 !important;
}


/* ========== CodeMirror ========== */

/* The editor is the folder body: outlined, its top edge shared with the header row */
.program-panel .cm-editor {
  background: transparent !important;
  border: 1px solid var(--wc-glass-border);
  border-radius: 0 0 var(--wc-radius-sm) var(--wc-radius-sm);
}
.program-panel .cm-editor .cm-gutters { background: transparent !important; }
/* Room for the last line to scroll clear of the fade and the playback bar */
.program-panel .cm-editor .cm-content { padding-bottom: 16px; }

/* Style CodeMirror's internal scrollbar */
.cm-scroller::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}

.cm-scroller::-webkit-scrollbar-thumb {
  background: var(--wc-state-selected);
  border-radius: 3px;
}

.cm-scroller::-webkit-scrollbar-thumb:hover { background: var(--wc-text-muted); }

/* CodeMirror line flash animation for newly added lines */
@keyframes cm-line-flash {
  0% { background-color: color-mix(in srgb, var(--wc-positive) 60%, transparent); }
  100% { background-color: transparent; }
}
.cm-line.cm-line-flash {
  animation: cm-line-flash var(--wc-duration-flash) var(--wc-ease-enter) forwards;
}

/* Persistent highlight on the line the running program is executing */
.cm-line.cm-highlighted {
  background-color: var(--wc-editor-exec-line);
}

/* LLM-proposed edits — strikethrough on removed lines, a widget for
   additions. Rendered by EditorDecorations._diff_decoration_specs. */
.cm-line.cm-edit-remove {
  background-color: var(--wc-error-soft);
  text-decoration: line-through;
  text-decoration-color: var(--wc-error);
}
.cm-edit-add {
  background-color: var(--wc-positive-soft);
  color: var(--wc-text);
  padding: 0 4px;
  border-left: 3px solid var(--wc-positive);
  white-space: pre;
}

/* Lines a recording session wrote that nobody has kept yet, and a badge on
   each captured span. */
.cm-line.cm-line-staged {
  background-color: color-mix(in srgb, var(--wc-fill-warning) 12%, transparent);
  box-shadow: inset 3px 0 0 color-mix(in srgb, var(--wc-fill-warning) 70%, transparent);
}
.cm-staged-badge {
  margin-left: 10px;
  padding: 0 6px;
  border-radius: var(--wc-radius-pill);
  font-size: 11px;
  color: var(--wc-fill-warning);
  background-color: color-mix(in srgb, var(--wc-fill-warning) 15%, transparent);
}

/* Pending-edit review cluster — swaps in for the editor toolbar buttons. */
.pending-edits-banner {
  background-color: var(--wc-positive-soft);
  border: 1px solid var(--wc-glass-border);
  border-radius: var(--wc-radius-sm);
  padding: 0 2px 0 10px;
}
.pending-edits-banner.staged-take {
  background-color: color-mix(in srgb, var(--wc-fill-warning) 8%, transparent);
  border-color: color-mix(in srgb, var(--wc-fill-warning) 30%, transparent);
}


/* Fade the code out at the bottom of the editor, inside its outline */
.program-panel .cm-scroller {
  -webkit-mask-image: linear-gradient(to bottom, black 0%, black calc(100% - 16px), transparent 100%);
  mask-image: linear-gradient(to bottom, black 0%, black calc(100% - 16px), transparent 100%);
}


/* ========== Editor Splitter/Playback Bar ========== */

/* Editor splitter styling with visible separator */
.editor-splitter {
  overflow: visible !important;
  min-height: 0;
}

/* Make splitter separator hold the playbar as handle */
.editor-splitter .q-splitter__separator {
  background: transparent !important;
  min-height: 48px !important;
  margin: -16px 0;
}

.editor-splitter .q-splitter__separator-area { background: transparent !important; }

/* Bottom playback bar: glass pill */
.bottom-playback-bar {
  border-radius: var(--wc-radius-pill);
  padding: 0 var(--wc-space-3);
}

/* Ensure playbar buttons remain clickable inside splitter separator */
.editor-splitter .bottom-playback-bar {
  cursor: default;
}

/* Timeline slider: transparent track, full-height hit area, line cursor on drag */
.timeline-slider .q-slider__track-container--h { background: transparent !important; }
.timeline-slider .q-slider__track { background: transparent !important; height: 100% !important; }
.timeline-slider .q-slider__inner { height: 100% !important; }
.timeline-slider .q-slider__focus-ring { display: none !important; }
.timeline-slider .q-slider__thumb::after {
  content: ''; position: absolute; width: 10px; height: 34px;
  background: var(--wc-text); border-radius: 4px; pointer-events: none;
  top: 50%; left: 50%; transform: translate(-50%, -50%);
  opacity: 0; transition: opacity var(--wc-duration-fast) var(--wc-ease-enter);
}
.timeline-slider .q-slider__thumb:hover::after { opacity: 0.4; }
.timeline-slider.q-slider--active .q-slider__thumb::after { opacity: 1; }
.scrub-track { background: var(--wc-well); }


/* ========== Editor Log Area ========== */

.program-panel .nicegui-log .q-scrollarea__content {
  padding: 16px 0px !important;
}

/* Log area rounded bottom corners */
.editor-splitter .q-splitter__after .nicegui-scroll-area { border-radius: 0 0 var(--wc-radius-sm) var(--wc-radius-sm); }

/* Fade log content at top using mask - fades in from transparent */
.editor-splitter .q-splitter__after {
  -webkit-mask-image: linear-gradient(to bottom, transparent 0%, black 16px, black 100%);
  mask-image: linear-gradient(to bottom, transparent 0%, black 16px, black 100%);
}


/* ========== Mobile Adjustments ========== */

/* Phone screens - hide left tabs, center right panels */
@media (max-width: 640px) {
  /* Hide left tab bar and panels completely */
  .side-tab-bar {
    display: none !important;
    visibility: hidden !important;
    opacity: 0 !important;
    width: 0 !important;
    height: 0 !important;
    overflow: hidden !important;
  }
  .left-panels-container { display: none !important; }

  /* Centred above the footer; the scale below keeps the bottom edge. */
  .overlay-br {
    right: auto !important;
    left: 50% !important;
    transform: translateX(-50%) !important;
    bottom: calc(var(--wc-footer-clearance) + max(0px, (100vw - 360px) * 0.0375)) !important;
  }
}

/* Small phone screens - scale control panel to fit */
/* Using stepped breakpoints since CSS can't compute unitless scale from viewport units */
@media (max-width: 414px) {
  .overlay-br {
    transform: translateX(-50%) scale(0.95) !important;
    transform-origin: center bottom !important;
  }
}

@media (max-width: 380px) {
  .overlay-br {
    transform: translateX(-50%) scale(0.88) !important;
    transform-origin: center bottom !important;
  }
}

@media (max-width: 340px) {
  .overlay-br {
    transform: translateX(-50%) scale(0.8) !important;
    transform-origin: center bottom !important;
  }
}

/* Transition for overlay panels on resize */
@media (min-width: 641px) {
  .overlay-br {
    transition: transform var(--wc-duration-base) var(--wc-ease-enter), left var(--wc-duration-base) var(--wc-ease-enter), right var(--wc-duration-base) var(--wc-ease-enter), width var(--wc-duration-base) var(--wc-ease-enter);
  }
}


/* ========== Recording Notification ========== */
/* The standing Recording notice sits with the cards, under the left panels */
.q-notifications__list:has(.recording-notification) {
  z-index: var(--wc-z-cards) !important;
}

.recording-notification .q-notification__icon {
  animation: recording-pulse var(--wc-duration-ambient) var(--wc-ease-loop) infinite;
}

@keyframes recording-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}


/* ========== Help Menu/Tutorial ========== */
.tutorial-dialog-card {
  width: 800px;
  max-width: 95vw;
  height: 85vh;
  max-height: 900px;
  min-height: 500px;
  overflow: hidden;
}

.tutorial-scroll .q-scrollarea__content {
  margin: 0 !important;
  padding: 0 !important;
}

.tutorial-dialog-card .q-stepper,
.tutorial-dialog-card .q-stepper__content {
  background: transparent !important;
}

/* ========== Keyboard Key Styling ========== */
.kbd-key {
  display: inline-block;
  padding: 3px 8px;
  font-family: var(--wc-font-sans);
  font-size: 11px;
  font-weight: 500;
  line-height: 1.2;
  letter-spacing: 0.02em;
  color: var(--wc-text);
  background: var(--wc-state-hover);
  border: 1px solid var(--wc-glass-border);
  border-radius: 4px;
  min-width: 20px;
  text-align: center;
  white-space: nowrap;
}

.kbd-plus {
  margin: 0 4px;
  color: var(--wc-text-muted);
  font-size: 0.7rem;
}

.kbd-group {
  display: inline-flex;
  align-items: center;
}

.keys-cell { padding: 6px 16px 6px 0 !important; }

.keybindings-table tbody td { border: none !important; }
.keybindings-table { background: transparent !important; }


/* ========== File Dialogs ========== */
.file-upload .q-uploader__header,
.file-upload .q-uploader__list {
  border-radius: var(--wc-radius-sm);
}
.file-upload .q-uploader__list { background: var(--wc-well); }
.file-upload {
  border-radius: var(--wc-radius-sm) !important;
  border: none !important;
}

.file-tree-scroll .q-scrollarea__content { padding: 0 !important; }

/* ========== File Tree ========== */
.file-tree .q-tree__node--selected > .q-tree__node-header .q-tree__node-header-content { font-weight: bold !important; }
.file-tree .q-tree__node--parent > .q-tree__node-header .q-tree__node-header-content { font-weight: bold !important; }

/* ========== Robot Face Indicator ========== */

/* The face is drawn in the chip's text colour; eyes and mouth are cut-outs
   in the surface behind it. */
.robot-face { --face-cut: var(--wc-glass-end); }
.bg-wc-mode-sim .robot-face { --face-cut: var(--wc-mode-sim); }

/* Robot face SVG transitions */
.robot-face .pupil { transition: transform 0.45s ease; }
.robot-face .eye-white { transition: opacity 0.25s ease; }

/* Breathing animations */
@keyframes breathe-happy {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-2px); }
}
@keyframes breathe-neutral {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-1.5px); }
}
@keyframes breathe-sad {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-2.5px); }
}
.robot-face-happy svg { animation: breathe-happy 6s ease-in-out infinite; }
.robot-face-neutral svg { animation: breathe-neutral 7s ease-in-out infinite; animation-delay: -2s; }
.robot-face-sad svg { animation: breathe-sad 8s ease-in-out infinite; animation-delay: -4s; }


/* ========== Takeover Overlay ========== */

/* Wandering sad robot — DVD-screensaver-style bounce around the viewport.
   Dimensions and fixed positioning are load-bearing: robot-faces.js uses
   FACE_SIZE = 96 for collision math, and the JS sets `transform` directly
   to compose translate + rotate without browser animation interference. */
.takeover-face {
  position: fixed;
  top: 0;
  left: 0;
  width: 96px;
  height: 96px;
  pointer-events: none;
}


/* ========== Status footer ========== */

.status-footer {
  position: absolute;
  left: var(--wc-space-3);
  right: var(--wc-space-3);
  bottom: var(--wc-space-3);
  height: var(--wc-size-footer);
  z-index: var(--wc-z-rail-bottom);
  display: flex;
  align-items: center;
  gap: var(--wc-space-2);
  padding: 0 var(--wc-space-1) 0 0;
  border-radius: var(--wc-radius-pill) !important;
  pointer-events: auto;
  white-space: nowrap;
  overflow: hidden;
}
.status-footer .q-chip {
  margin: 0;
  height: calc(var(--wc-size-footer) - 4px);
  box-shadow: none;
}
.status-footer .footer-mode { border-radius: var(--wc-radius-pill); padding: 0 8px 0 4px; }
.status-footer .footer-mode .robot-face { width: 20px; height: 20px; flex-shrink: 0; }
.status-footer .footer-mode .robot-face svg { width: 20px; height: 20px; display: block; }
.status-footer .footer-mode .q-chip__content { gap: 4px; flex-wrap: nowrap; }
.status-footer .readout-robot-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.status-footer .footer-tool { max-width: 160px; }
.status-footer .footer-tool .q-chip__content { min-width: 0; }
.status-footer .footer-sep { width: 1px; height: 14px; background: var(--wc-glass-border); flex-shrink: 0; }
.status-footer .io-dots { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.io-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--wc-control);
  transition: background var(--wc-duration-fast) var(--wc-ease-enter);
}
.io-dot.io-dot-on { background: var(--wc-action); }
.status-footer .pose-well {
  display: flex; align-items: center; gap: 6px;
  height: calc(var(--wc-size-footer) - 6px);
  padding: 0 10px;
  border-radius: var(--wc-radius-pill);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.status-footer .pose-well .wc-caption { line-height: 1; }
.status-footer .pose-cell { display: flex; align-items: baseline; gap: 6px; }
.status-footer .pose-value { display: inline-block; text-align: center; }
.status-footer .footer-action {
  flex: 1 1 0; min-width: 0;
  overflow: hidden; text-overflow: ellipsis;
  cursor: pointer;
}
.status-footer .footer-action .action-icon { font-size: 13px; vertical-align: -2px; margin-right: 3px; }
.footer-history { min-width: 240px; max-width: 420px; padding: 4px 0; }
.footer-history .action-log-entry { padding: 2px 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.status-footer .footer-btn {
  min-height: calc(var(--wc-size-footer) - 6px) !important;
  min-width: 0 !important;
  padding: 0 8px !important;
  border-radius: var(--wc-radius-pill);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.status-footer .footer-btn .q-icon { font-size: 15px; }
.status-footer .footer-btn .footer-count { margin: 0 6px 0 2px; }
.status-footer .footer-btn.unread-warning { background: var(--wc-warning-soft) !important; color: var(--wc-warning) !important; }
.status-footer .footer-btn.unread-error { background: var(--wc-error-soft) !important; color: var(--wc-error) !important; }
/* The launchers never shrink, so the pose gives way as the window narrows:
   first its rotations, then the whole well. */
@media (max-width: 1280px) { .status-footer .pose-cell-rot { display: none; } }
@media (max-width: 960px) { .status-footer .pose-well { display: none; } }
/* Settings has the rail's gear above phone width. */
.status-footer .footer-settings { display: none; }
@media (max-width: 640px) { .status-footer .footer-settings { display: inline-flex; } }
.footer-io-label, .footer-empty-tool { display: none; }

/* The editor's 450px minimum, controls and rail no longer fit side by side. */
@media (max-width: 960px) {
  :root {
    --wc-wrapped-footer-height: 112px;
    --wc-footer-clearance: min(var(--wc-wrapped-footer-height), max(28px, calc(100dvh - var(--wc-control-height, 248px) - 12px)));
  }
  .commander-workspace { height: 100dvh; }
  .commander-scene, .workspace-overlays { display: none !important; }
  .status-footer {
    left: auto;
    right: var(--wc-space-3);
    bottom: 0;
    width: 426px;
    max-width: 100%;
    height: var(--wc-footer-clearance);
    overflow-y: auto;
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    grid-template-rows: 26px 18px 18px 22px 26px;
    column-gap: 0;
    row-gap: 0;
    padding: 0 6px;
    border-radius: var(--wc-radius-sm) !important;
  }
  .status-footer .footer-mode { grid-area: 1 / 1 / 2 / 5; justify-self: start; }
  .status-footer .readout-robot-name { grid-area: 1 / 5 / 2 / 7; }
  .status-footer .footer-tool, .status-footer .footer-empty-tool { grid-area: 1 / 7 / 2 / 10; max-width: 100%; }
  .status-footer .footer-empty-tool { display: block; }
  .status-footer .footer-btn:not(.footer-log):not(.footer-settings) { grid-area: 1 / 10 / 2 / 12; padding: 0 !important; }
  .status-footer .footer-btn .footer-count { margin: 0 2px 0 1px; }
  .status-footer .footer-settings { display: inline-flex; grid-area: 1 / 12 / 2 / 13; padding: 0 !important; }
  .status-footer .footer-sep { display: none; }
  .status-footer .pose-well { display: contents; }
  .status-footer .pose-cell { display: flex; min-width: 0; gap: 4px; }
  .status-footer .pose-value { min-width: 0 !important; }
  .status-footer .footer-pose-x { grid-area: 2 / 1 / 3 / 5; }
  .status-footer .footer-pose-y { grid-area: 2 / 5 / 3 / 9; }
  .status-footer .footer-pose-z { grid-area: 2 / 9 / 3 / 13; }
  .status-footer .footer-pose-rx { grid-area: 3 / 1 / 4 / 5; }
  .status-footer .footer-pose-ry { grid-area: 3 / 5 / 4 / 9; }
  .status-footer .footer-pose-rz { grid-area: 3 / 9 / 4 / 13; }
  .status-footer .io-dots { grid-area: 4 / 1 / 5 / 9; min-width: 0; overflow-x: auto; }
  .status-footer .footer-io-label { display: block; }
  .status-footer .io-dot { flex-shrink: 0; }
  .status-footer .footer-speed { grid-area: 4 / 9 / 5 / 13; justify-content: end; }
  .status-footer .footer-action { grid-area: 5 / 1 / 6 / 11; }
  .status-footer .footer-log { grid-area: 5 / 11 / 6 / 13; justify-self: end; }
}
@media (max-width: 640px) {
  .status-footer { left: 0; right: 0; width: 100%; }
}
/* Use the extra width in short landscape viewports before requiring scrolling. */
@media (min-width: 500px) and (max-width: 960px) and (max-height: 440px) {
  :root { --wc-wrapped-footer-height: 60px; }
  .status-footer {
    left: 0;
    right: 0;
    width: 100%;
    grid-template-rows: 22px 16px 20px;
  }
  .status-footer .footer-mode { grid-area: 1 / 1 / 2 / 4; height: 20px; }
  .status-footer .readout-robot-name { grid-area: 1 / 4 / 2 / 5; font-size: 11px; padding-right: 2px; }
  .status-footer .footer-tool, .status-footer .footer-empty-tool { grid-area: 1 / 5 / 2 / 7; }
  .status-footer .io-dots { grid-area: 1 / 7 / 2 / 10; }
  .status-footer .footer-pose-x { grid-area: 2 / 1 / 3 / 3; }
  .status-footer .footer-pose-y { grid-area: 2 / 3 / 3 / 5; }
  .status-footer .footer-pose-z { grid-area: 2 / 5 / 3 / 7; }
  .status-footer .footer-pose-rx { grid-area: 2 / 7 / 3 / 9; }
  .status-footer .footer-pose-ry { grid-area: 2 / 9 / 3 / 11; }
  .status-footer .footer-pose-rz { grid-area: 2 / 11 / 3 / 13; }
  .status-footer .footer-action { grid-area: 3 / 1 / 4 / 9; }
  .status-footer .footer-speed { grid-area: 3 / 9 / 4 / 12; }
  .status-footer .footer-log { grid-area: 3 / 12 / 4 / 13; min-height: 20px !important; }
}

/* ========== Bottom panel ========== */

.bottom-panel {
  position: absolute;
  right: calc(min(var(--wc-control-inset, 0px), 50vw) + var(--wc-space-3));
  bottom: var(--wc-footer-clearance);
  left: var(--wc-size-panel-inset);
  height: var(--wc-size-bottom-panel);
  max-height: calc(100vh - var(--wc-footer-clearance) - var(--wc-space-3));
  z-index: var(--wc-z-panels);
  display: flex;
  flex-direction: column;
  padding: 0;
  pointer-events: auto;
}
/* Under an open column the panel shrinks to leave the column its minimum,
   which keeps the playbar and its Stop on screen. */
body:has(.panels-wrap.column-open) .bottom-panel {
  max-height: calc(100vh - var(--wc-footer-clearance) - 2 * var(--wc-space-3) - var(--wc-size-column-min));
}
.bottom-panel .bottom-panel-tabs { flex-shrink: 0; }
.bottom-panel .bottom-panel-tabs .q-tab { min-height: 36px; padding: 0 14px; }
.bottom-panel .q-tab-panels { flex: 1 1 0; min-height: 0; }
.bottom-panel .q-tab-panel { height: 100%; padding: var(--wc-space-2) var(--wc-space-3); overflow: auto; }
.bottom-panel .nicegui-log { height: 100%; }
@media (max-width: 960px) {
  .bottom-panel, body:has(.panels-wrap.column-open) .bottom-panel {
    top: var(--wc-space-3);
    left: var(--wc-space-3);
    right: var(--wc-space-3);
    height: auto;
    max-height: none;
  }
}
.diag-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 0 var(--wc-space-4); width: 100%; }
.diag-grid > .diag-col { min-width: 0; }
.diag-grid > .diag-wide { grid-column: 1 / -1; min-width: 0; }

/* Diagnostics event log. Unlike the one-line action log these entries wrap:
   the cause, effect and remedy are the parts worth reading, so they get the
   room a one-line strip cannot give them. */
.diag-event {
  font-size: 12px;
  line-height: 1.45;
  padding: 3px 0;
  border-bottom: 1px solid var(--wc-glass-border);
}
.diag-event:last-child { border-bottom: none; }
.diag-event .material-symbols-outlined {
  font-size: 14px;
  vertical-align: -2px;
  margin-right: 3px;
}
.diag-event-time { color: var(--wc-text-muted); margin-right: 5px; }
.diag-event-code { color: var(--wc-text-muted); margin-left: 5px; }
.diag-event-detail { color: var(--wc-text-muted); padding-left: 22px; }
.diag-event-remedy { padding-left: 22px; font-style: italic; }

/* ========== Reduced motion ========== */
@media (prefers-reduced-motion: reduce) {
  .control-glow-breathe, .ai-cluster .btn-take-control, .recording-notification .q-notification__icon,
  .record-btn.recording .q-icon, .robot-face-happy svg, .robot-face-neutral svg, .robot-face-sad svg,
  .tab-flash, .cm-line.cm-line-flash, .handeye-coverage-next { animation: none !important; }
  .left-panels-container .q-panel.scroll[class*="q-transition--slide"] { animation-duration: 0s !important; }
}
"""
    )

    ui.add_css(_RESIZE_HANDLE_CSS)
    ui.add_head_html('<script src="/static/js/panel-resize.js" defer></script>')
