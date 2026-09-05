import logging
from typing import Any, Literal, cast, get_args

from nicegui import app, ui

logger = logging.getLogger(__name__)

ThemeMode = Literal["light", "dark", "system"]


# Centralized color constants. Browser CSS uses Tailwind v4 CSS variables;
# Three.js/scene colors use hex values (Three.js can't read CSS variables).


# Semantic status colors (for notifications, badges, status indicators)
class StatusColors:
    """Status indicator colors using Tailwind variables."""

    POSITIVE = "var(--color-emerald-500)"  # Success, connected, enabled
    NEGATIVE = "var(--color-red-500)"  # Error, disconnected, danger
    WARNING = "var(--color-yellow-500)"  # Warning, caution
    INFO = "var(--color-sky-500)"  # Information, neutral action
    ACCENT = "var(--color-sky-500)"  # Brand accent (same as info)
    MUTED = "var(--color-neutral-400)"  # Disabled, inactive


# I/O indicator colors (Quasar color names for q-badge/q-icon)
IO_COLOR_ON = "green-8"
IO_COLOR_OFF = "grey-7"


# 3D Scene / visualization colors (hex for Three.js)
class SceneColors:
    """Colors for 3D scene elements and visualizations.

    Axis colors are CVD-aware (colorblind-friendly) and follow robotics convention.
    """

    # Axis colors (X=red, Y=green, Z=blue) - CVD-aware values
    AXIS_X_HEX = "#d94c3f"
    AXIS_Y_HEX = "#2faf7a"
    AXIS_Z_HEX = "#4a63e0"
    # Lighter variants for rotational axes
    AXIS_RX_HEX = "#f1a79f"
    AXIS_RY_HEX = "#aee5cf"
    AXIS_RZ_HEX = "#aeb9f3"

    # Background colors
    BACKGROUND_DARK_HEX = "#151515"
    BACKGROUND_LIGHT_HEX = "#e0e0e0"

    # Ground plane color (contrasts with background)
    GROUND_DARK_HEX = "#202020"
    GROUND_LIGHT_HEX = "#d4d4d4"

    # Material colors (robot mesh)
    MATERIAL_DARK_HEX = "#a3a3a3"
    MATERIAL_LIGHT_HEX = "#737373"

    # Simulator mode amber
    SIM_AMBER_HEX = "#c77d28"

    # Edit mode / inactive gray
    EDIT_GRAY_HEX = "#525252"

    # Predicted-collision highlight (theme-independent deep alarm red)
    COLLISION_HEX = "#b00020"

    # Keep-out shape / barrier (translucent slate — red highlight stands out)
    SHAPE_HEX = "#6d8ea0"
    # Program shape awaiting backend readback confirmation (amber = unconfirmed)
    SHAPE_DRAFT_HEX = "#d9a05b"
    # Installation-layer shape (robot config; not editable from the GUI)
    SHAPE_INSTALL_HEX = "#55606a"
    # Proposed installation shape: drafted here, enforced only once it is in
    # the robot config (violet — neither program slate nor installation grey)
    SHAPE_PROPOSED_HEX = "#8a7bb5"

    # Tool body colors (teal family — distinct from arm in every mode)
    TOOL_BODY_HEX = "#2a9d8f"
    TOOL_BODY_SIM_HEX: str = TOOL_BODY_HEX
    TOOL_BODY_EDIT_HEX = "#3d6b65"

    # Tool moving part colors (lighter/brighter teal variant)
    TOOL_MOVING_HEX = "#4ecdc4"
    TOOL_MOVING_SIM_HEX: str = TOOL_MOVING_HEX
    TOOL_MOVING_EDIT_HEX = "#4d7e77"

    # Derived colors (reference base colors)
    ENVELOPE_HEX = AXIS_Z_HEX
    TCP_ACTIVE_HEX = AXIS_Z_HEX
    TCP_INACTIVE_HEX = EDIT_GRAY_HEX


# Path visualization colors
class PathColors:
    """Colors for path/trajectory visualization."""

    CARTESIAN = "#10b981"  # emerald-500 - cartesian/linear moves
    JOINTS = "#2563eb"  # blue-600 - joint space moves
    SMOOTH = "#a855f7"  # purple-500 - smooth moves (move_c, move_s)
    INVALID = "#ef4444"  # red-500 - IK failure / unreachable
    TIMING_WARNING = "#f59e0b"  # amber-500 - needs more time than requested
    CHECKPOINT = "#94a3b8"  # slate-400 - checkpoint markers on scrubber
    TOOL_ACTION = "#FF9800"  # orange - tool action bars on scrubber


# Move type to color mapping (for path_visualizer and path_preview_client)
MOVE_TYPE_COLORS: dict[str, str] = {
    "cartesian": PathColors.CARTESIAN,
    "joints": PathColors.JOINTS,
    "smooth": PathColors.SMOOTH,
    "smooth_arc": PathColors.SMOOTH,
    "smooth_spline": PathColors.SMOOTH,
    "jog": PathColors.CARTESIAN,
    "invalid": PathColors.INVALID,
    "timing_warning": PathColors.TIMING_WARNING,
    "unknown": PathColors.CARTESIAN,
}


def get_color_for_move_type(move_type: str, is_valid: bool = True) -> str:
    """Get the visualization color for a move type."""
    if not is_valid:
        return MOVE_TYPE_COLORS["invalid"]

    move_type_lower = move_type.lower() if move_type else "unknown"

    if move_type_lower in MOVE_TYPE_COLORS:
        return MOVE_TYPE_COLORS[move_type_lower]

    if "smooth" in move_type_lower:
        return MOVE_TYPE_COLORS["smooth"]
    if "joint" in move_type_lower:
        return MOVE_TYPE_COLORS["joints"]
    if "cartesian" in move_type_lower or "pose" in move_type_lower:
        return MOVE_TYPE_COLORS["cartesian"]

    return MOVE_TYPE_COLORS["unknown"]


def get_palette(mode: ThemeMode) -> dict[str, str]:
    """Return CTk-mapped palette tokens for the given mode."""
    # Semantic colors are identical across light/dark.
    semantic = {
        "accent": StatusColors.ACCENT,
        "positive": StatusColors.POSITIVE,
        "negative": StatusColors.NEGATIVE,
        "info": StatusColors.INFO,
        "warning": StatusColors.WARNING,
    }

    if mode == "dark":
        return {
            "primary": "var(--color-sky-700)",
            "primary_hover": "var(--color-sky-800)",
            "background": "var(--color-neutral-900)",
            "surface": "var(--color-neutral-800)",
            "surface_top": "var(--color-neutral-700)",
            "text": "var(--color-neutral-300)",
            "muted": "var(--color-neutral-400)",
            "seg_unselected": "var(--color-neutral-600)",
            "on_primary": "var(--color-slate-100)",
            **semantic,
        }
    return {
        "primary": "var(--color-sky-700)",
        "primary_hover": "var(--color-sky-800)",
        "background": "var(--color-neutral-200)",
        "surface": "var(--color-neutral-300)",
        "surface_top": "var(--color-neutral-400)",
        "text": "var(--color-neutral-900)",
        "muted": "var(--color-neutral-500)",
        "seg_unselected": "var(--color-neutral-400)",
        "on_primary": "var(--color-slate-100)",
        **semantic,
    }


def _inject_tailwind_colors() -> None:
    """Inject hidden element with Tailwind classes to force JIT to generate color vars.

    Tailwind JIT only emits CSS for classes it sees used, so referencing every
    --color-* swatch here keeps them available to the rest of the app.
    """
    colors = [
        "slate",
        "gray",
        "zinc",
        "neutral",
        "stone",
        "red",
        "orange",
        "amber",
        "yellow",
        "lime",
        "green",
        "emerald",
        "teal",
        "cyan",
        "sky",
        "blue",
        "indigo",
        "violet",
        "purple",
        "fuchsia",
        "pink",
        "rose",
    ]
    shades = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]
    classes = ["bg-white", "bg-black"]
    classes.extend(f"bg-{color}-{shade}" for color in colors for shade in shades)
    ui.add_head_html(f'<div style="display:none" class="{" ".join(classes)}"></div>')


def _inject_css_vars(p: dict[str, str]) -> None:
    """Inject global CSS variables and basic background/text mappings."""
    ui.add_css(
        f"""
:root {{
  --ctk-primary: {p["primary"]};
  --ctk-primary-hover: {p["primary_hover"]};
  --ctk-bg: {p["background"]};
  --ctk-surface: {p["surface"]};
  --ctk-surface-top: {p["surface_top"]};
  --ctk-text: {p["text"]};
  --ctk-muted: {p["muted"]};
  --ctk-on-primary: {p["on_primary"]};
  --ctk-seg-unselected: {p["seg_unselected"]};

  /* Axis/TCP colors - CVD-aware (from SceneColors) */
  --axis-x: {SceneColors.AXIS_X_HEX};
  --axis-rx: {SceneColors.AXIS_RX_HEX};
  --axis-y: {SceneColors.AXIS_Y_HEX};
  --axis-ry: {SceneColors.AXIS_RY_HEX};
  --axis-z: {SceneColors.AXIS_Z_HEX};
  --axis-rz: {SceneColors.AXIS_RZ_HEX};

  /* Glass tint colors (swapped in light mode) */
  --glass-tint: var(--color-neutral-100);
  --glass-shadow-tint: var(--color-neutral-900);

  /* Glass defaults (computed from tint) */
  --glass-blur: 36px;
  --glass-bg-1: color-mix(in srgb, var(--glass-tint) 16%, transparent);
  --glass-bg-2: color-mix(in srgb, var(--glass-tint) 8%, transparent);
  --glass-border: color-mix(in srgb, var(--glass-tint) 18%, transparent);
  --glass-shadow: color-mix(in srgb, var(--glass-shadow-tint) 35%, transparent);
  --glass-fg: var(--ctk-text);
  --glass-hover: color-mix(in srgb, var(--glass-tint) 8%, transparent);

  /* Unified overlay variables (computed from tint) */
  --overlay-bg-1: color-mix(in srgb, var(--glass-tint) 20%, transparent);
  --overlay-bg-2: color-mix(in srgb, var(--glass-tint) 10%, transparent);
  --overlay-stroke-light: var(--color-white);
  --overlay-stroke-dark: var(--color-black);
  --overlay-reflex-light: 1;
  --overlay-reflex-dark: 0.6;
  --overlay-saturation: 150%;

  /* Shared glass box-shadow (liquid-glass reflex effect) */
  --glass-box-shadow:
    inset -0.3px -1px 4px 0 color-mix(in srgb, var(--overlay-stroke-dark) calc(var(--overlay-reflex-dark) * 12%), transparent),
    inset -1.5px 2.5px 0 -2px color-mix(in srgb, var(--overlay-stroke-dark) calc(var(--overlay-reflex-dark) * 18%), transparent),
    inset 0 3px 4px -2px color-mix(in srgb, var(--overlay-stroke-dark) calc(var(--overlay-reflex-dark) * 18%), transparent),
    0 6px 16px 0 color-mix(in srgb, var(--overlay-stroke-dark) calc(var(--overlay-reflex-dark) * 8%), transparent);

  /* Shared backdrop-filter for glass elements */
  --glass-backdrop: blur(var(--glass-blur)) saturate(var(--overlay-saturation));

  /* UI element colors (handles, tabs, scrollbars) - swapped in light mode */
  --ui-tint: #fff;
  --ui-tint-muted: #aaa;
  --handle-bg: rgba(255, 255, 255, 0.2);
  --handle-bg-hover: rgba(255, 255, 255, 0.45);
  --tab-bg: rgba(255, 255, 255, 0.08);
  --tab-bg-hover: rgba(255, 255, 255, 0.12);
  --tab-bg-active: rgba(255, 255, 255, 0.18);
  --scrollbar-thumb: rgba(255, 255, 255, 0.2);
  --scrollbar-thumb-hover: rgba(255, 255, 255, 0.35);

  /* Flash animation color */
  --flash-color: var(--color-emerald-500);

  /* Semantic brand tokens - using Tailwind v4 CSS variables */
  --sem-danger: var(--color-red-500);
  --sem-warning: var(--color-amber-400);
  --sem-success: var(--color-emerald-500);
  --sem-info: var(--color-sky-500);
  --brand-accent: var(--color-sky-500);

  /* Simulator mode amber - used for arm ghosting and toggle button */
  --sim-amber: var(--color-amber-600);

  /* On-color defaults for legibility */
  --on-danger: var(--color-white);
  --on-warning: var(--color-neutral-900);
  --on-success: var(--color-neutral-900);
  --on-info: var(--color-neutral-900);
  --on-accent: var(--color-neutral-900);

  /* Joint bar height */
  --joint-bar-h: 33px;
}}

body, .q-page {{ background: var(--ctk-bg); color: var(--ctk-text); }}

/* Flip colors in light mode */
body.body--light {{
  /* Glass tints */
  --glass-tint: var(--color-neutral-900);
  --glass-shadow-tint: var(--color-neutral-900);
  --glass-fg: var(--color-slate-100);
  --overlay-reflex-light: 0.6;
  --overlay-reflex-dark: 1.2;
  --overlay-saturation: 160%;

  /* UI element colors (inverted for light mode) */
  --ui-tint: #000;
  --ui-tint-muted: #666;
  --handle-bg: rgba(0, 0, 0, 0.15);
  --handle-bg-hover: rgba(0, 0, 0, 0.35);
  --tab-bg: rgba(0, 0, 0, 0.06);
  --tab-bg-hover: rgba(0, 0, 0, 0.10);
  --tab-bg-active: rgba(0, 0, 0, 0.14);
  --scrollbar-thumb: rgba(0, 0, 0, 0.2);
  --scrollbar-thumb-hover: rgba(0, 0, 0, 0.35);
}}

/* Ensure component-scoped dark contexts inherit glass defaults */
.q-dark {{
  --glass-tint: var(--color-neutral-100);
  --glass-shadow-tint: var(--color-neutral-900);
  --glass-fg: var(--ctk-text);
}}
"""
    )


def _inject_component_overrides() -> None:
    """Inject component-specific overrides to mimic CustomTkinter visual behavior."""
    ui.add_css(
        """
/* Containers and surfaces */
.q-header, .q-footer { background: var(--ctk-surface); color: var(--ctk-text); }
.q-field, .q-toolbar, .q-item { background: var(--ctk-surface); color: var(--ctk-text); }
.overlay-panel > .q-card { background: var(--ctk-surface); color: var(--ctk-text); }

/* Buttons */
.q-btn:not(.q-btn--round) { border-radius: 6px; padding-top: 3px !important; padding-left: 6px !important; padding-bottom: 3px !important; padding-right: 6px !important; min-height: 32px !important; min-width: 32px !important; }
.q-btn.bg-primary:hover { background: var(--ctk-primary-hover) !important; }
.q-btn--flat, .q-btn--outline { color: var(--ctk-text); }
.q-slider__thumb { width: 30px !important; height: 30px !important; }
.q-slider__track { height: 8px !important; }

/* Inputs */
.q-input .q-field__native, .q-textarea .q-field__native { color: var(--ctk-text); }
.joint-readout-input .q-field__native { padding-top: 12px !important; padding-bottom: 4px !important; }
.q-field__control { border-radius: 6px; }
.step-input .q-field__suffix { display: inline-block; width: 14px; text-align: center; }
.step-suffix-small .q-field__suffix { font-size: 0.7em; }

/* Segmented toggle */
.q-btn-toggle .q-btn { border-radius: 6px; }
.q-btn-toggle .q-btn.q-btn--active { background: var(--ctk-primary); color: var(--ctk-on-primary); }
.q-btn-toggle .q-btn:not(.q-btn--active) { background: var(--ctk-seg-unselected); color: var(--ctk-on-primary); }

/* Misc */
.q-separator { background: var(--ctk-muted); }

/* Transparent shell for frosted glass overlay panels */
body.body--dark, body.body--light { background: transparent !important; }
.q-page { background: transparent !important; }
.q-field { background: transparent !important; }
/* Panel container (q-tab-panels) is transparent — each tab panel provides its own glass */
.left-panels-container { background: transparent !important; }
/* Nested q-tab-panels inside overlay cards are transparent (the card provides the glass) */
.overlay-card .q-tab-panels { background: transparent !important; }

/* Disable input steppers for numercal input */
input::-webkit-outer-spin-button,
input::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}
input[type=number] {
  -moz-appearance: textfield;
}

/* Strong disabled utility for controls */
.cp-disabled-strong {
  opacity: 0.15 !important;
  filter: grayscale(1) contrast(0.6) brightness(0.8);
  pointer-events: none !important;
  cursor: not-allowed !important;
  box-shadow: none !important;
}
"""
    )


def apply_theme(mode: ThemeMode) -> None:
    """
    Apply the selected theme:
    - Set NiceGUI/Quasar colors and dark mode.
    - Inject CTk CSS variables and component overrides.
    """
    choice = mode
    if mode == "system":
        choice = "dark" if ui.dark_mode().client.page.dark else "light"
        logger.debug("System theme: %s", choice)

    pal = get_palette(choice)

    # Quasar color tokens (primary/secondary/accent) and feedback colors
    ui.colors(
        primary=pal["primary"],
        secondary=pal["primary_hover"],
        accent=pal["accent"],
        positive=pal["positive"],
        negative=pal["negative"],
        info=pal["info"],
        warning=pal["warning"],
    )

    if choice == "dark":
        ui.dark_mode().enable()
    else:
        ui.dark_mode().disable()

    _inject_tailwind_colors()
    _inject_css_vars(pal)
    _inject_component_overrides()


def set_theme(mode: ThemeMode) -> ThemeMode:
    """Persist, set and apply theme mode."""
    app.storage.general["theme_mode"] = mode
    apply_theme(mode)
    return mode


def get_theme() -> ThemeMode:
    """Return current requested mode ('light'/'dark'/'system')."""
    mode = app.storage.general.get("theme_mode", "system")
    if isinstance(mode, str) and mode in get_args(ThemeMode):
        return cast("ThemeMode", mode)
    return cast("ThemeMode", "system")


def is_dark_theme() -> bool:
    """Return True if the effective theme is not light."""
    return get_theme() != "light"


def toggle_theme() -> ThemeMode:
    """Cycle through modes: system -> light -> dark -> system."""
    order: list[ThemeMode] = ["system", "light", "dark"]
    current = get_theme()
    try:
        idx = order.index(current)
    except ValueError:
        idx = 0
    next_mode: ThemeMode = order[(idx + 1) % len(order)]
    set_theme(next_mode)
    return next_mode


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
  transition: background 0.15s ease, width 0.15s ease, height 0.15s ease;
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
  transition: background 0.15s ease, width 0.15s ease, height 0.15s ease;
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

/* Axis/TCP colors */
.tcp-x  { color: var(--axis-x); }
.tcp-rx { color: var(--axis-rx); }
.tcp-y  { color: var(--axis-y); }
.tcp-ry { color: var(--axis-ry); }
.tcp-z  { color: var(--axis-z); }
.tcp-rz { color: var(--axis-rz); }


/* ========== Controls ========== */

/* Cartesian jog buttons: a fixed-size hit box hosting a currentColor SVG
   glyph with the axis label overlaid as HTML (assets carry no text). */
.cart-jog-slot {
  position: relative;
  width: 72px;
  height: 72px;
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
  color: #000;
  -webkit-text-stroke: 0.09em #000;
  pointer-events: none;
  user-select: none;
}

/* Pressed visual feedback for jog controls */
.is-pressed {
  transform: scale(0.96);
  filter: brightness(1.2);
  outline: 1px solid var(--q-accent);
  transition: transform 40ms linear, filter 40ms linear, outline-color 40ms linear;
}

/* Joint control bars with integrated pill buttons */
.joint-bar {
  border-radius: 9999px !important;
  height: var(--joint-bar-h);
}

.joint-cap {
  height: calc(var(--joint-bar-h) - 1px);
  width: var(--joint-bar-h);
  min-height: 0;
  padding: 0;
  border-radius: 9999px;
  background: transparent !important;
  color: var(--ui-tint) !important;
  font-size: 19px;
}

.joint-cap:hover {
  opacity: 0.8;
}

.joint-cap.q-btn--disabled {
  color: var(--ui-tint-muted) !important;
  pointer-events: none;
}

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


/* ========== Overlays ========== */

/* AI-driving perimeter glow: breathes while an AI session holds the lease */
@keyframes wc-glow-breathe {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}
.control-glow-breathe { animation: wc-glow-breathe 2.6s ease-in-out infinite; }

/* ---- AI control cluster ----
   One --mode-accent per control mode themes the perimeter glow and the
   top-center capsule. No amber/orange accents here — amber is the
   physical arm's color (see .consent-hw below). */
.wc-mode-inspect    { --mode-accent: var(--color-emerald-400); --mode-accent-text: var(--color-emerald-300); }
.wc-mode-auto-edits { --mode-accent: var(--color-sky-400);     --mode-accent-text: var(--color-sky-300); }
.wc-mode-autopilot  { --mode-accent: var(--color-violet-400);  --mode-accent-text: var(--color-violet-300); }
body.body--light .wc-mode-inspect    { --mode-accent-text: var(--color-emerald-700); }
body.body--light .wc-mode-auto-edits { --mode-accent-text: var(--color-sky-700); }
body.body--light .wc-mode-autopilot  { --mode-accent-text: var(--color-violet-700); }

/* CSS-variable scope over glow + capsule; generates no box, so the fixed
   children still position against the viewport. */
.ai-mode-scope { display: contents; }

.control-lease-glow {
  position: fixed; inset: 0; pointer-events: none; z-index: 9998;
  box-shadow: inset 0 0 18px 2px color-mix(in srgb, var(--mode-accent) 30%, transparent),
              inset 0 0 60px 8px color-mix(in srgb, var(--mode-accent) 12%, transparent);
}
/* An MCP client is connected but the human drives. */
.control-lease-glow.glow-faint { opacity: 0.35; }

/* Glass capsule holding the mode chip + Take-control button. Its own
   backdrop-filter makes it a containing block — fine while it has no
   position:fixed descendants (the glow is a sibling). */
.ai-cluster {
  position: fixed; top: 8px; left: 50%; transform: translateX(-50%); z-index: 9999;
  display: flex; align-items: center; gap: 4px; padding: 3px 4px; border-radius: 9999px;
  background: linear-gradient(135deg, var(--overlay-bg-1), var(--overlay-bg-2));
  backdrop-filter: var(--glass-backdrop);
  -webkit-backdrop-filter: var(--glass-backdrop);
  border: 1px solid color-mix(in srgb, var(--mode-accent) 35%, transparent);
  box-shadow: var(--glass-box-shadow);
  isolation: isolate;
  color: var(--glass-fg);
  transition: border-color 0.3s ease;
}
.ai-cluster.ai-driving { border-color: color-mix(in srgb, var(--mode-accent) 65%, transparent); }

/* Text-only at rest so the capsule reads as one pill (no pill-in-pill);
   the hover tint is the click affordance. */
.ai-cluster .control-mode-chip {
  background: transparent !important;
  color: var(--mode-accent-text) !important;
  border-radius: 9999px; font-weight: 500; margin: 0;
}
.ai-cluster .control-mode-chip:hover {
  background: color-mix(in srgb, var(--mode-accent) 15%, transparent) !important;
}

/* The only solid-filled element in the capsule: pops out when the AI takes
   the lease (the entry animation replays on every hidden -> visible flip)
   and pulses on the glow-breathe clock. */
.ai-cluster .btn-take-control {
  background: var(--mode-accent) !important;
  color: var(--color-neutral-900) !important;
  border-radius: 9999px; font-weight: 600;
  /* Chip-height so the capsule doesn't grow when the button pops in. */
  font-size: 0.75rem; min-height: 0; padding: 1px 10px;
  animation: wc-popout 0.28s cubic-bezier(0.34, 1.56, 0.64, 1),
             wc-btn-pulse 2.6s ease-in-out infinite;
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

/* Approval dialog: a standard app glass panel — mode accents stay in the
   top capsule. Allow is an ordinary primary button; the hardware consent
   variant goes amber = the physical arm. */
.ai-approval-card {
  min-width: 320px; max-width: 440px;
  background: linear-gradient(135deg, var(--overlay-bg-1), var(--overlay-bg-2)) !important;
  backdrop-filter: var(--glass-backdrop);
  -webkit-backdrop-filter: var(--glass-backdrop);
  border: 1px solid var(--glass-border);
  border-radius: 10px; color: var(--glass-fg);
  box-shadow: var(--glass-box-shadow);
}
.ai-approval-card .ai-approval-desc {
  background: color-mix(in srgb, currentColor 7%, transparent);
  border-left: 2px solid var(--ctk-primary);
  border-radius: 4px; padding: 6px 10px;
}
.ai-approval-card .btn-consent-allow {
  background: var(--ctk-primary) !important;
  color: var(--ctk-on-primary) !important;
}
.ai-approval-card .btn-consent-allow:hover {
  background: var(--ctk-primary-hover) !important;
}
.ai-approval-card.consent-hw .ai-approval-icon { color: var(--sim-amber); }
.ai-approval-card.consent-hw .ai-approval-desc { border-left-color: var(--sim-amber); }
.ai-approval-card.consent-hw .btn-consent-allow {
  background: var(--sim-amber) !important;
  color: var(--on-warning) !important;
}

@media (prefers-reduced-motion: reduce) {
  .control-glow-breathe, .ai-cluster .btn-take-control { animation: none !important; }
}

/* Overlay panels with frosted glass effect */
.overlay-panel { position: absolute; z-index: 10; pointer-events: auto; }
.overlay-card {
  padding: 10px;
  border-radius: 10px;
  background: linear-gradient(135deg, var(--overlay-bg-1), var(--overlay-bg-2)) !important;
  backdrop-filter: var(--glass-backdrop);
  -webkit-backdrop-filter: var(--glass-backdrop);
  border: 0 !important;
  box-shadow: var(--glass-box-shadow);
  isolation: isolate;
}

/* Overlay anchors */
.overlay-tl { top: 12px; left: 12px; }
.overlay-tr { top: 12px; right: 12px; }
.overlay-bl { bottom: 12px; left: 12px; }
.overlay-br { bottom: 12px; right: 12px; }
.overlay-right {
  position: absolute;
  top: 50%;
  right: 12px;
  transform: translateY(-50%);
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 12;
}


/* ========== Left Tabs ========== */

/* Reduce vertical tab padding to match right side panel margins (12px) */
.q-tabs--vertical .q-tab {
  padding: 8px 12px !important;
  min-height: 44px !important;
}

/* Make left tab column narrower */
.q-tabs--vertical {
  width: 52px !important;
}

/* Side tab bar with frosted glass effect - unified bar appearance */
.side-tab-bar {
  background: linear-gradient(135deg, var(--overlay-bg-1), var(--overlay-bg-2)) !important;
  backdrop-filter: var(--glass-backdrop);
  -webkit-backdrop-filter: var(--glass-backdrop);
  border: 0 !important;
  border-radius: 10px;
  box-shadow: var(--glass-box-shadow);
  isolation: isolate;
  margin: 12px;
  padding: 4px 0;
  pointer-events: auto;
  height: auto !important;
  min-height: 0 !important;
}

/* Ensure tabs inside the bar have proper sizing */
.side-tab-bar .q-tab {
  min-height: 44px !important;
  padding: 8px 12px !important;
}

/* Editor tab flash animation for new content */
@keyframes tab-flash {
  0%, 50% { background-color: color-mix(in srgb, var(--flash-color) 40%, transparent); }
  25%, 75% { background-color: transparent; }
  100% { background-color: transparent; }
}
.tab-flash {
  animation: tab-flash 1s ease-out 2;
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

.top-panels-container { top: 12px;}

.bottom-panels-container { bottom: 12px; }

.resizable-panel { overflow: hidden !important; }

/* Panel content is interactive when visible */
.left-panels-container .overlay-card { pointer-events: auto; }

/* Tab panels appear to come from underneath with left edge shadow */
.top-panels-container .q-tab-panel.overlay-card {
  border-top-left-radius: 0 !important;
  border-bottom-left-radius: 12px !important;
  box-shadow:
    inset 4px 0 8px -4px color-mix(in srgb, var(--overlay-stroke-dark) calc(var(--overlay-reflex-dark) * 25%), transparent),
    var(--glass-box-shadow);
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
  animation-duration: 0.3s !important;
  animation-timing-function: ease-out !important;
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
 .nicegui-log .log-trace   { color: var(--sem-info);    opacity: 0.8; }
 .nicegui-log .log-debug   { color: var(--ctk-muted);   opacity: 0.9; }
 .nicegui-log .log-info    { color: var(--ctk-text); }
 .nicegui-log .log-warning { color: var(--sem-warning); }
 .nicegui-log .log-error   { color: var(--sem-danger); }
 .nicegui-log .log-critical {
   color: var(--on-danger);
   background: var(--sem-danger);
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
  background: var(--handle-bg);
  border-radius: 2px;
}

[class*="resize-handle-"]:hover::after,
[class*="resize-handle-"].dragging::after { background: var(--handle-bg-hover); }

/* ========== Editor Tabs ========== */

/* Editor tabs container */
.editor-tabs .q-tab {
  padding: 4px 8px !important;
  min-height: 36px !important;
  text-transform: none !important;
}

/* Individual editor tab styling */
.editor-tab {
  background: var(--tab-bg);
  border-radius: 6px 6px 0 0;
  margin-right: 2px;
  transition: background 0.15s ease;
}

.editor-tab:hover { background: var(--tab-bg-hover); }

.editor-tab.q-tab--active {
  background: var(--tab-bg-active);
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
.editor-tab .q-field { min-height: 24px !important; }

.editor-tab .q-field__control {
  height: 24px !important;
  min-height: 24px !important;
}

.editor-tab .q-field__native {
  padding: 0 4px !important;
  min-height: 20px !important;
  font-size: 0.85rem;
}

/* Compact save FAB in tabs */
.editor-tab .save-fab {
  min-width: 24px !important;
  min-height: 24px !important;
  width: 24px !important;
  height: 24px !important;
}

.editor-tab .save-fab .q-icon { font-size: 14px !important; }

/* Editor tabs scroll area - no padding */
.editor-tabs-scroll .q-scrollarea__content {
  padding: 0 !important;
  gap: 0 !important;
}


/* ========== CodeMirror ========== */

/* CodeMirror editor needs to fill available space */
.program-panel .cm-editor {
  border-radius: 12px 12px 0 0;
  padding-bottom: 16px;
}

/* Round the top left corner of the gutter */
.program-panel .cm-editor .cm-gutters { border-top-left-radius: 12px; }

/* Style CodeMirror's internal scrollbar */
.cm-scroller::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}

.cm-scroller::-webkit-scrollbar-thumb {
  background: var(--scrollbar-thumb);
  border-radius: 3px;
}

.cm-scroller::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-thumb-hover); }

/* CodeMirror line flash animation for newly added lines */
@keyframes cm-line-flash {
  0% { background-color: color-mix(in srgb, var(--flash-color) 60%, transparent); }
  100% { background-color: transparent; }
}
.cm-line.cm-line-flash {
  animation: cm-line-flash 1.5s ease-out forwards;
}

/* Persistent highlight applied to the currently-executing script line.
   Matches the rgba(255, 255, 0, 0.3) yellow used by the original
   highlight_lines() built-in style. */
.cm-line.cm-highlighted {
  background-color: rgba(255, 255, 0, 0.3);
}

/* LLM-proposed edits — red strikethrough on removed lines, green
   widget for additions. Rendered by EditorDecorations._diff_decoration_specs. */
.cm-line.cm-edit-remove {
  background-color: rgba(244, 67, 54, 0.18);
  text-decoration: line-through;
  text-decoration-color: rgba(244, 67, 54, 0.7);
}
.cm-edit-add {
  background-color: rgba(76, 175, 80, 0.18);
  color: var(--ctk-text, #cbd5e1);
  padding: 0 4px;
  border-left: 3px solid rgba(76, 175, 80, 0.7);
  white-space: pre;
}

/* Pending-edit review cluster — swaps in for the editor toolbar buttons. */
.pending-edits-banner {
  background-color: rgba(76, 175, 80, 0.08);
  border: 1px solid rgba(76, 175, 80, 0.25);
  border-radius: 6px;
  padding: 0 2px 0 10px;
}


/* Fade CodeMirror content at bottom using mask - fades to transparent */
.editor-tab-panel {
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

/* Bottom playback bar */
.bottom-playback-bar {
  background-color: var(--overlay-bg-1) !important;
  backdrop-filter: blur(16px) saturate(var(--overlay-saturation));
  -webkit-backdrop-filter: blur(16px) saturate(var(--overlay-saturation));
  border-radius: 9999px;
  box-shadow: var(--glass-box-shadow);
  padding: 0 12px;
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
  background: #424242; border-radius: 4px; pointer-events: none;
  top: 50%; left: 50%; transform: translate(-50%, -50%);
  opacity: 0; transition: opacity 0.15s ease;
}
.timeline-slider .q-slider__thumb:hover::after { opacity: 0.4; }
.timeline-slider.q-slider--active .q-slider__thumb::after { opacity: 1; }


/* ========== Editor Log Area ========== */

.program-panel .nicegui-log .q-scrollarea__content {
  padding: 16px 0px !important;
}

/* Log area rounded bottom corners */
.editor-splitter .q-splitter__after .nicegui-scroll-area { border-radius: 0 0 12px 12px; }

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
    transition: transform 0.3s ease, left 0.3s ease, right 0.3s ease, width 0.3s ease;
  }
}


/* ========== Recording Notification ========== */
/* Override parent container z-index when it contains recording notification */
.q-notifications__list:has(.recording-notification) {
  z-index: 15 !important;
}

.recording-notification .q-notification__icon {
  animation: recording-pulse 2s ease-in-out infinite;
}

@keyframes recording-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}


/* ========== Help Menu/Tutorial ========== */
.tutorial-dialog-card {
  background: linear-gradient(135deg, var(--overlay-bg-1), var(--overlay-bg-2)) !important;
  backdrop-filter: var(--glass-backdrop);
  -webkit-backdrop-filter: var(--glass-backdrop);
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
  font-family: system-ui, -apple-system, sans-serif;
  font-size: 0.75rem;
  font-weight: 500;
  line-height: 1.2;
  color: var(--ctk-text);
  background: linear-gradient(180deg, rgba(255,255,255,0.12) 0%, rgba(255,255,255,0.04) 100%);
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 4px;
  box-shadow:
    0 1px 0 rgba(0,0,0,0.3),
    inset 0 1px 0 rgba(255,255,255,0.08);
  min-width: 20px;
  text-align: center;
  white-space: nowrap;
}

.kbd-plus {
  margin: 0 4px;
  color: var(--ctk-muted);
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
  border-radius: 12px;
}
.file-upload {
  border-radius: 12px !important;
  border: none !important;
}
.file-upload .q-uploader__header {
  box-shadow: 0 -8px 16px rgba(0, 0, 0, 0.4);
}

.file-tree-scroll .q-scrollarea__content { padding: 0 !important; }

/* ========== File Tree ========== */
.file-tree .q-tree__node-header-content { color: white !important; }
.file-tree .q-tree__node--selected > .q-tree__node-header .q-tree__node-header-content { color: white !important; font-weight: bold !important; }
.file-tree .q-tree__node--parent > .q-tree__node-header .q-tree__node-header-content { font-weight: bold !important; }

/* ========== Robot Face Indicator ========== */

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
  transition: max-height 0.2s ease;
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
"""
    )

    ui.add_css(_RESIZE_HANDLE_CSS)
    ui.add_head_html('<script src="/static/js/panel-resize.js" defer></script>')
