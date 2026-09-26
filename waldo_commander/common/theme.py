"""Theme: turns the design tokens into CSS variables, Quasar colour names and
hex values, and holds the app's component and layout CSS.

Every colour in the app resolves through a token in ``tokens.py``:

- CSS reads ``var(--wc-<name>)`` (dark values on ``:root``, light on ``body.body--light``).
- Quasar ``color=`` / ``text-color=`` props use the registered name ``wc-<name>``.
- Three.js and ECharts read hex from :func:`hex_of`; vertex colours use :func:`rgb01`.
"""

import logging
from typing import Any, Literal

from nicegui import ui

from waldo_commander.common import tokens as T

logger = logging.getLogger(__name__)

ThemeMode = Literal["light", "dark", "system"]
ThemeKey = Literal["dark", "light"]


# ── Token adapters ───────────────────────────────────────────────────


def css(name: str) -> str:
    """CSS value for a token, for classes and inline styles."""
    return f"var(--wc-{name})"


def quasar(name: str) -> str:
    """Quasar colour name for a token, for ``color=`` and ``text-color=`` props."""
    return f"wc-{name}"


def hex_of(name: str, theme: ThemeKey | None = None) -> str:
    """Opaque hex for a token in the effective (or given) theme, for Three.js and charts."""
    return T.COLOR_HEX[name][theme or effective_theme()]


def rgb01(name: str, theme: ThemeKey | None = None) -> list[float]:
    """A token as an RGB triple in 0–1, for Three.js vertex colours."""
    h = hex_of(name, theme).lstrip("#")
    return [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]


class SceneColors:
    """Hex colours for the 3D scene, read from the dark tokens.

    Theme-dependent surfaces (background, ground, arm) should go through
    :func:`hex_of` at the point of use; these constants serve code and tests
    that need a fixed value.
    """

    AXIS_X_HEX = T.COLOR_HEX["axis-x"]["dark"]
    AXIS_Y_HEX = T.COLOR_HEX["axis-y"]["dark"]
    AXIS_Z_HEX = T.COLOR_HEX["axis-z"]["dark"]
    AXIS_RX_HEX = T.COLOR_HEX["axis-rx"]["dark"]
    AXIS_RY_HEX = T.COLOR_HEX["axis-ry"]["dark"]
    AXIS_RZ_HEX = T.COLOR_HEX["axis-rz"]["dark"]

    BACKGROUND_DARK_HEX = T.COLOR_HEX["scene-bg"]["dark"]
    BACKGROUND_LIGHT_HEX = T.COLOR_HEX["scene-bg"]["light"]
    GROUND_DARK_HEX = T.COLOR_HEX["scene-ground"]["dark"]
    GROUND_LIGHT_HEX = T.COLOR_HEX["scene-ground"]["light"]
    GRID_DARK_HEX = T.COLOR_HEX["scene-grid"]["dark"]
    GRID_LIGHT_HEX = T.COLOR_HEX["scene-grid"]["light"]
    MATERIAL_DARK_HEX = T.COLOR_HEX["scene-arm"]["dark"]
    MATERIAL_LIGHT_HEX = T.COLOR_HEX["scene-arm"]["light"]

    SIM_AMBER_HEX = T.COLOR_HEX["scene-arm-sim"]["dark"]
    EDIT_GRAY_HEX = T.COLOR_HEX["scene-arm-edit"]["dark"]
    COLLISION_HEX = T.COLOR_HEX["scene-collision"]["dark"]

    SHAPE_HEX = T.COLOR_HEX["scene-shape"]["dark"]
    SHAPE_DRAFT_HEX = T.COLOR_HEX["scene-shape-draft"]["dark"]
    SHAPE_INSTALL_HEX = T.COLOR_HEX["scene-shape-install"]["dark"]
    SHAPE_PROPOSED_HEX = T.COLOR_HEX["scene-shape-proposed"]["dark"]

    TOOL_BODY_HEX = T.COLOR_HEX["scene-tool"]["dark"]
    TOOL_BODY_SIM_HEX = T.COLOR_HEX["scene-tool"]["dark"]
    TOOL_BODY_EDIT_HEX = T.COLOR_HEX["scene-tool-edit"]["dark"]
    TOOL_MOVING_HEX = T.COLOR_HEX["scene-tool-moving"]["dark"]
    TOOL_MOVING_SIM_HEX = T.COLOR_HEX["scene-tool-moving"]["dark"]
    TOOL_MOVING_EDIT_HEX = T.COLOR_HEX["scene-tool-moving-edit"]["dark"]

    HOVER_HEX = T.COLOR_HEX["scene-hover"]["dark"]
    ENVELOPE_HEX = AXIS_Z_HEX
    TCP_ACTIVE_HEX = AXIS_Z_HEX
    TCP_INACTIVE_HEX = EDIT_GRAY_HEX


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
    """Segment colour for a move type in the effective theme.

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
        elif "joint" in key:
            key = "joints"
        elif "cartesian" in key or "pose" in key:
            key = "cartesian"
        else:
            key = "unknown"
    return hex_of(_MOVE_TYPE_TOKENS[key])


# ── CSS generation ───────────────────────────────────────────────────


def _color_value(name: str, theme: ThemeKey) -> str:
    v = T.COLOR[name][theme]
    return f"var(--wc-{v[1:-1]})" if v.startswith("{") else v


def _token_block(theme: ThemeKey) -> str:
    lines = [f"  --wc-{n}: {_color_value(n, theme)};" for n in T.COLOR]
    lines.append(f"  --wc-shadow-glass: {T.SHADOW['shadow-glass'][theme]};")
    return "\n".join(lines)


def _scalar_block() -> str:
    lines = []
    for group in (
        T.SPACE,
        T.RADIUS,
        T.SIZE,
        T.EFFECT,
        T.OPACITY,
        T.Z_INDEX,
        T.DURATION,
        T.EASING,
    ):
        lines.extend(f"  --wc-{n}: {v};" for n, v in group.items())
    lines.extend(f"  --wc-font-{n}: {v};" for n, v in T.FONT_FAMILY.items())
    return "\n".join(lines)


def _type_classes() -> str:
    rules = []
    for name, (size, lh, weight, spacing) in T.TYPE_STYLE.items():
        family = "var(--wc-font-mono)" if name == "code" else "var(--wc-font-sans)"
        extra = f" letter-spacing: {spacing};" if spacing else ""
        if name.startswith("readout"):
            extra += " font-variant-numeric: tabular-nums;"
        rules.append(
            f".wc-{name} {{ font-family: {family}; font-size: {size}; line-height: {lh};"
            f" font-weight: {weight};{extra} }}"
        )
    return "\n".join(rules)


def _inject_tokens_css() -> None:
    """Emit every token as ``--wc-*``: dark on ``:root``, light on ``body.body--light``."""
    ui.add_css(
        f"""
:root {{
{_token_block("dark")}
{_scalar_block()}
}}

body.body--light {{
{_token_block("light")}
}}

{_type_classes()}
"""
    )


_GLASS_SELECTORS = (
    ".glass, .overlay-card, .side-tab-bar, .ai-cluster, .ai-approval-card,"
    " .tutorial-dialog-card, .bottom-playback-bar, .q-dialog__inner > .q-card, .q-menu"
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
.q-btn.disabled {{ opacity: var(--wc-opacity-disabled) !important; }}
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
.q-field__label {{ color: var(--wc-text-muted); }}
.q-field:not(.q-field--borderless) .q-field__control {{
  background: var(--wc-well);
  border-radius: var(--wc-radius-sm);
}}
.q-field--error .q-field__bottom, .q-field--error .q-field__messages {{ color: var(--wc-error) !important; }}
.joint-readout-input .q-field__native {{ padding-top: 12px !important; padding-bottom: 4px !important; }}
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


_applied_theme: ThemeKey = "dark"


def apply_theme(mode: ThemeMode) -> None:
    """Register the tokens with Quasar, set dark mode, and inject the CSS."""
    global _applied_theme
    choice: ThemeKey
    if mode == "system":
        choice = "dark" if ui.dark_mode().client.page.dark else "light"
        logger.debug("System theme: %s", choice)
    else:
        choice = mode
    _applied_theme = choice

    ui.colors(
        primary=css("action"),
        secondary=css("action-hover"),
        accent=css("action-text"),
        dark=css("glass-end"),
        dark_page=css("scene-bg"),
        positive=css("fill-positive"),
        negative=css("fill-error"),
        info=css("action"),
        warning=css("fill-warning"),
        **{quasar(name): css(name) for name in T.COLOR},
    )

    if choice == "dark":
        ui.dark_mode().enable()
    else:
        ui.dark_mode().disable()

    _inject_tokens_css()
    _inject_component_overrides()


def effective_theme() -> ThemeKey:
    """The theme :func:`apply_theme` last put on the page, so hex values for the
    scene and charts always agree with the CSS variables."""
    return _applied_theme


# Panel resize configuration (passed to JS module)
PANEL_RESIZE_CONFIG: dict[str, Any] = {
    "storageKey": "parol_panel_sizes",
    "selectors": {
        "wrap": ".panels-wrap",
        "topContainer": ".top-panels-container",
        "bottomContainer": ".bottom-panels-container",
    },
    "constraints": {
        "viewportMarginX": 80,
        "viewportMarginY": 20,
        "containerPadding": 20,
        "bottomOffset": 12,
        "totalMargin": 36,
    },
    "stateClasses": {
        "coupled": "coupled",
    },
    "panels": {
        "program": {
            "selector": ".top-panels-container .program-panel",
            "minWidth": 450,
            "minHeight": 300,
            "group": "top",
        },
        "response": {
            "selector": ".bottom-panels-container .response-panel",
            "minWidth": 300,
            "minHeight": 100,
            "group": "bottom",
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

/* Joint readout input — compact field styling */
.joint-readout-input .q-field__control {
  max-height: 3em !important;
}

.joint-readout-input .q-field__native {
   padding: 0 !important;
}

.joint-readout-input .q-field__label {
    top: 12px !important;
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

/* Joint control bars: control track, progress travel, the value crossing both under a scrim halo */
.joint-bar {
  border-radius: var(--wc-radius-pill) !important;
  height: var(--wc-size-joint-bar);
}
.joint-bar .q-linear-progress__track { opacity: 1; }

/* Settings rows fill the panel, never the widest child */
.settings-scroll .q-scrollarea__content { width: 100%; min-width: 0; }
.joint-value-pill {
  padding: 0 var(--wc-space-2);
  color: var(--wc-text);
  font-variant-numeric: tabular-nums;
  text-shadow: 0 0 3px var(--wc-scrim), 0 0 3px var(--wc-scrim);
}

.joint-cap {
  height: calc(var(--wc-size-joint-bar) - 1px);
  width: var(--wc-size-joint-bar);
  min-height: 0;
  padding: 0;
  border-radius: var(--wc-radius-pill);
  color: var(--wc-text) !important;
  font-size: 19px;
}

.joint-cap:hover {
  opacity: 0.8;
}

.joint-cap.q-btn--disabled {
  color: var(--wc-text-disabled) !important;
  pointer-events: none;
}

/* Level indicators: the ten dots run a hue ramp; Quasar dims unselected dots to 40% */
.level-speed .q-rating__icon-container:nth-child(1) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 0%) !important; }
.level-speed .q-rating__icon-container:nth-child(2) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 11%) !important; }
.level-speed .q-rating__icon-container:nth-child(3) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 22%) !important; }
.level-speed .q-rating__icon-container:nth-child(4) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 33%) !important; }
.level-speed .q-rating__icon-container:nth-child(5) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 44%) !important; }
.level-speed .q-rating__icon-container:nth-child(6) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 56%) !important; }
.level-speed .q-rating__icon-container:nth-child(7) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 67%) !important; }
.level-speed .q-rating__icon-container:nth-child(8) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 78%) !important; }
.level-speed .q-rating__icon-container:nth-child(9) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 89%) !important; }
.level-speed .q-rating__icon-container:nth-child(10) .q-icon { color: color-mix(in oklch, var(--wc-level-speed-lo), var(--wc-level-speed-hi) 100%) !important; }
.level-accel .q-rating__icon-container:nth-child(1) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 0%) !important; }
.level-accel .q-rating__icon-container:nth-child(2) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 11%) !important; }
.level-accel .q-rating__icon-container:nth-child(3) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 22%) !important; }
.level-accel .q-rating__icon-container:nth-child(4) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 33%) !important; }
.level-accel .q-rating__icon-container:nth-child(5) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 44%) !important; }
.level-accel .q-rating__icon-container:nth-child(6) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 56%) !important; }
.level-accel .q-rating__icon-container:nth-child(7) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 67%) !important; }
.level-accel .q-rating__icon-container:nth-child(8) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 78%) !important; }
.level-accel .q-rating__icon-container:nth-child(9) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 89%) !important; }
.level-accel .q-rating__icon-container:nth-child(10) .q-icon { color: color-mix(in oklch, var(--wc-level-accel-lo), var(--wc-level-accel-hi) 100%) !important; }

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

/* Record button: a control with a record dot that pulses while recording */
.record-btn .q-icon { color: var(--wc-record); }
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
.overlay-panel { position: absolute; z-index: var(--wc-z-panels); pointer-events: auto; }
.overlay-card {
  padding: var(--wc-space-3);
}

/* Overlay anchors */
.overlay-tl { top: var(--wc-space-3); left: var(--wc-space-3); }
.overlay-tr { top: var(--wc-space-3); right: var(--wc-space-3); }
.overlay-bl { bottom: var(--wc-space-3); left: var(--wc-space-3); }
.overlay-br { bottom: var(--wc-space-3); right: var(--wc-space-3); }
.overlay-right {
  position: absolute;
  top: 50%;
  right: var(--wc-space-3);
  transform: translateY(-50%);
  display: flex;
  flex-direction: column;
  gap: var(--wc-space-2);
  z-index: var(--wc-z-panels);
}


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
.side-tab-bar.absolute.bottom-0 { z-index: var(--wc-z-rail-bottom); }

/* Ensure tabs inside the bar have proper sizing */
.side-tab-bar .q-tab {
  min-height: 44px !important;
  padding: 8px 12px !important;
}

/* Editor tab flash animation for new content */
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
  left: 58px;
  max-width: calc(100vw - 80px);
  overflow: hidden !important;
  scrollbar-width: none !important;
  -ms-overflow-style: none !important;
}

.left-panels-container::-webkit-scrollbar { display: none !important; }

.top-panels-container { top: var(--wc-space-3); }

.bottom-panels-container { bottom: var(--wc-space-3); }

.resizable-panel { overflow: hidden !important; }

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
  text-transform: none !important;
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

/* Compact save FAB in tabs */
.editor-tab .save-fab {
  min-width: var(--wc-size-control-sm) !important;
  min-height: var(--wc-size-control-sm) !important;
  width: var(--wc-size-control-sm) !important;
  height: var(--wc-size-control-sm) !important;
}

.editor-tab .save-fab .q-icon { font-size: 14px !important; }

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

/* Pending-edit review cluster — swaps in for the editor toolbar buttons. */
.pending-edits-banner {
  background-color: var(--wc-positive-soft);
  border: 1px solid var(--wc-glass-border);
  border-radius: var(--wc-radius-sm);
  padding: 0 2px 0 10px;
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

  /* Center panels horizontally using transform */
  .overlay-tr {
    right: auto !important;
    left: 50% !important;
    transform: translateX(-50%) !important;
    /* Variable top margin that goes to 0 on small screens */
    top: max(0px, calc((100vw - 360px) * 0.0375)) !important;
    /* Prevent text wrapping, scale down instead */
    white-space: nowrap !important;
    font-size: clamp(0.65rem, 2.8vw, 1rem) !important;
  }

  .overlay-br {
    right: auto !important;
    left: 50% !important;
    transform: translateX(-50%) !important;
    /* Variable bottom margin that goes to 0 on small screens */
    bottom: max(0px, calc((100vw - 360px) * 0.0375)) !important;
  }
}

/* Small phone screens - scale control panel to fit */
/* Using stepped breakpoints since CSS can't compute unitless scale from viewport units */
@media (max-width: 414px) {
  .overlay-br, .overlay-tr {
    transform: translateX(-50%) scale(0.95) !important;
    transform-origin: center bottom !important;
  }
}

@media (max-width: 380px) {
  .overlay-br, .overlay-tr {
    transform: translateX(-50%) scale(0.88) !important;
    transform-origin: center bottom !important;
  }
}

@media (max-width: 340px) {
  .overlay-br, .overlay-tr {
    transform: translateX(-50%) scale(0.8) !important;
    transform-origin: center bottom !important;
  }
}

/* Transition for overlay panels on resize */
@media (min-width: 641px) {
  .overlay-tr, .overlay-br {
    transition: transform var(--wc-duration-base) var(--wc-ease-enter), left var(--wc-duration-base) var(--wc-ease-enter), right var(--wc-duration-base) var(--wc-ease-enter), width var(--wc-duration-base) var(--wc-ease-enter);
  }
}


/* ========== Recording Notification ========== */
/* Override parent container z-index when it contains recording notification */
.q-notifications__list:has(.recording-notification) {
  z-index: var(--wc-z-rail) !important;
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

/* Help dialog - expand to fit content */
.help-dialog-card {
  max-width: 95vw;
  max-height: 95vh;
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

.help-dialog-card .q-stepper,
.help-dialog-card .q-stepper__content {
  background: transparent !important;
}


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
.file-tree .q-tree__node-header-content { color: var(--wc-text) !important; }
.file-tree .q-tree__node--selected > .q-tree__node-header .q-tree__node-header-content { color: var(--wc-text) !important; font-weight: bold !important; }
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

/* Help tab has no panel — hide its indicator to prevent stale marker at startup */
.side-tab-bar.absolute.bottom-0 .q-tab:last-child .q-tab__indicator {
    display: none !important;
}


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


/* ========== Action Log ========== */

.action-log {
  max-height: 20px;
  transition: max-height var(--wc-duration-fast) var(--wc-ease-enter);
  width: 0;
  min-width: 100%;
  cursor: pointer;
}
/* Collapsed: no padding, no scrollbars, no user scroll */
.action-log:not(.action-log-expanded) .q-scrollarea__content { padding: 0 !important; }
.action-log:not(.action-log-expanded) .q-scrollarea__container { overflow: hidden !important; }
.action-log:not(.action-log-expanded) .q-scrollarea__thumb,
.action-log:not(.action-log-expanded) .q-scrollarea__bar {
  opacity: 0 !important;
  pointer-events: none !important;
}
/* Expanded: Quasar handles scrollbars natively, just override padding */
.action-log.action-log-expanded {
  max-height: 200px;
}
.action-log.action-log-expanded .q-scrollarea__content { padding: 2px 0 !important; }
.action-log-entry {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ========== Reduced motion ========== */
@media (prefers-reduced-motion: reduce) {
  .control-glow-breathe, .ai-cluster .btn-take-control, .recording-notification .q-notification__icon,
  .record-btn.recording .q-icon, .robot-face-happy svg, .robot-face-neutral svg, .robot-face-sad svg,
  .tab-flash, .cm-line.cm-line-flash { animation: none !important; }
  .left-panels-container .q-panel.scroll[class*="q-transition--slide"] { animation-duration: 0s !important; }
}
"""
    )

    ui.add_css(_RESIZE_HANDLE_CSS)
    ui.add_head_html('<script src="/static/js/panel-resize.js" defer></script>')
