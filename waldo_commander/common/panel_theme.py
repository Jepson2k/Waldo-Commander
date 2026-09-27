"""Shared presentation for task panels and measurement charts."""

from nicegui import ui

from waldo_commander.common.theme import hex_of


def joint_colors() -> list[str]:
    """One series colour per joint, from the active theme."""
    return [hex_of(f"chart-{i}") for i in range(1, 7)]


def chart_text() -> str:
    return hex_of("text-muted")


def chart_grid() -> str:
    return hex_of("control")


def inject_panel_css() -> None:
    ui.add_css("""
@layer quasar {
  /* A skill's form opens where the side panels do, clear of the scene, the
     readout and the E-stop. */
  .skill-dialog-host > .q-dialog__inner--left { align-items: flex-start !important; padding: 12px 0 12px 58px !important; }
}
.task-panel, .overlay-card.task-panel, .task-dialog {
  color: var(--wc-text);
  background: var(--wc-surface) !important;
  font-size: 14px;
}
.task-panel .q-tab, .task-dialog .q-tab { min-height: 36px; }
.task-panel .q-field__label, .task-dialog .q-field__label { font-size: 16px; }
.task-panel .text-caption, .task-dialog .text-caption { font-size: 12px; line-height: 1.45; }
.task-panel .q-expansion-item > .q-expansion-item__container > .q-item,
.task-dialog .q-expansion-item > .q-expansion-item__container > .q-item {
  min-height: 36px; padding: 4px 8px; background: transparent;
}
/* A flex column with the tab panel's full width (the tab panel starts its
   children, so without a width a plugin's content shrinks to its widest
   line), so a panel capped at the viewport shrinks its plugin's own scroller
   rather than scrolling the plugin whole, heading and all. */
.plugin-panel-content { display: flex; flex-direction: column; flex: 1 1 auto; width: 100%; height: 100%; min-height: 0; min-width: 0; overflow: auto; overflow-x: hidden; }
.panel-body { flex: 1 1 0; min-height: 0; min-width: 0; width: 100%; overflow-y: auto; overflow-x: hidden; }
.panel-heading { font-size: 16px; font-weight: 600; line-height: 24px; }
.panel-actions { flex-shrink: 0; width: 100%; align-items: center; gap: 8px; padding-top: 8px; }
.panel-note { color: var(--wc-text-muted); font-size: 12px; line-height: 1.45; }
.task-dialog { max-height: calc(100dvh - 32px); padding: 16px; gap: 12px; }
.task-dialog .q-card__section { min-width: 0; }
.task-dialog > .panel-body { flex-basis: auto; }
.task-panel .q-table th, .task-dialog .q-table th { color: var(--wc-text-muted); text-align: left; }
.task-panel .q-table td, .task-dialog .q-table td { text-align: left; }
.settings-content { flex: 1 1 auto; min-height: 0; width: 100%; gap: 0; flex-wrap: nowrap; overflow-y: auto; overflow-x: hidden; }
.settings-panel { max-width: calc(100vw - 80px); }
/* The rule under each heading is the only divider; a row is its label beside
   its control, and what the setting does is the label's tooltip. */
.settings-group-heading { width: 100%; margin-top: 12px; padding-bottom: 3px; border-bottom: 1px solid var(--wc-glass-border); font-size: 12px; font-weight: 600; color: var(--wc-text-muted); }
.settings-content > .settings-group-heading:first-child { margin-top: 0; }
.settings-row { display: grid; grid-template-columns: 150px minmax(0, 1fr); column-gap: 8px; align-items: center; min-height: 28px; width: 100%; }
.settings-row > .settings-label { font-size: 13px; line-height: 1.25; min-width: 0; color: var(--wc-text); }
.settings-row > :not(.settings-label) { justify-self: start; min-width: 0; max-width: 100%; }
/* Fields fill the control column so every row ends at the same edge. */
.settings-row > .q-field, .settings-row > .nicegui-grid, .settings-row > :has(> .q-field) { justify-self: stretch; width: auto; }
.settings-row > :not(.settings-label) > .q-field { width: auto; }
.settings-address { display: grid; grid-template-columns: minmax(0, 1fr) 72px; column-gap: 8px; justify-self: stretch; }
.settings-row .q-field__control, .settings-row .q-field__marginal { min-height: 28px; height: 28px; }
.settings-row .q-field__native, .settings-row .q-field__prefix, .settings-row .q-field__suffix { min-height: 28px; padding-top: 0; padding-bottom: 0; }
.settings-row .q-toggle__inner { font-size: 28px; }
.settings-axis { width: auto; }
.settings-axis .q-field__prefix { color: var(--wc-text-muted); padding-right: 4px; }
.diagnostics-view { width: 560px; max-width: calc(100vw - 80px); max-height: calc(100dvh - 24px); flex-wrap: nowrap; }
/* Normal is colourless, so anything with colour in it is asking for attention.
   The verdict is the one thing sized to be read from across the room. */
.diag-verdict { font-size: 17px; font-weight: 600; color: var(--wc-text); }
.diag-ok { color: var(--wc-text); }
.diag-warn { color: var(--wc-warning); }
.diag-fault { color: var(--wc-error); }
.diag-verdict.diag-ok { color: var(--wc-text); }
/* The period budget drawn as its full width, so how close the loop runs to
   its deadline is a position rather than a number to be compared from memory. */
.diag-bar { flex: 0 0 auto; width: 180px; height: 4px; margin: 3px 0 5px; border-radius: 2px; background: var(--wc-control); overflow: hidden; }
.diag-bar-fill { height: 100%; width: 0; border-radius: 2px; background: var(--wc-text-muted); transition: width .2s linear; }
.diag-bar-fill.over { background: var(--wc-warning); }
.diagnostics-view > .panel-body { flex-basis: auto; }
/* Calibration is four steps: the ribbon is where the operator is, the
   thumbnails are what they have, and a view's border is the only colour. */
.handeye-steps { gap: 0; border-bottom: 1px solid var(--wc-glass-border); }
.handeye-step { flex: 1 1 0; border-radius: 0; border-bottom: 2px solid transparent; color: var(--wc-text-muted) !important; }
.handeye-step.handeye-step-done { color: var(--wc-text) !important; }
.handeye-step.handeye-step-active { color: var(--wc-text) !important; border-bottom-color: var(--wc-action); }
.handeye-views { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 5px; }
.handeye-view { position: relative; aspect-ratio: 4 / 3; border: 1px solid var(--wc-glass-border); border-radius: 3px; overflow: hidden; background: var(--wc-scene-bg); }
.handeye-view .q-img { position: absolute; inset: 0; height: 100%; }
.handeye-view-index { position: absolute; left: 5px; top: 2px; font-size: 11px; color: var(--wc-text); text-shadow: 0 0 3px var(--wc-scene-bg); }
.handeye-view .q-btn { position: absolute; right: 0; top: 0; opacity: 0; }
.handeye-view:hover .q-btn { opacity: 1; }
.handeye-view-empty { border-style: dashed; background: transparent; }
.handeye-view-similar { border-color: var(--wc-warning); }
.handeye-view-error { border-color: var(--wc-error); }
.handeye-verdict { font-size: 17px; font-weight: 600; }
.handeye-camera-frame { position: relative; flex-shrink: 0; width: 100%; height: 210px; display: flex; justify-content: center; background: var(--wc-scene-bg); border: 1px solid var(--wc-glass-border); border-radius: 4px; overflow: hidden; }
.handeye-camera { height: 100%; max-width: 100%; }
.handeye-camera-chip { position: absolute; left: 8px; top: 8px; font-size: 11px; line-height: 1.4; padding: 2px 7px; border-radius: 3px; background: var(--wc-well); color: var(--wc-text-muted); }
.handeye-camera-chip-found { color: var(--wc-positive); }
.handeye-count { font-size: 15px; font-weight: 600; color: var(--wc-text); }
/* A skill is inserted from a dialog beside the 3D view, labelled by its diagram. */
.skill-dialog { width: 450px; max-width: calc(100vw - 80px); margin: 0; max-height: calc(100vh - 24px) !important; }
.skill-detail-icon { width: 42px; height: 30px; font-size: 30px; flex-shrink: 0; }
.skill-menu-icon { width: 34px; height: 24px; font-size: 24px; }
.editor-toolbar-menu { min-width: 190px; }
.editor-toolbar-menu .q-item { min-height: 36px; }
.event-detail-grid { display: grid; grid-template-columns: minmax(90px, 1fr) minmax(0, 3fr); gap: 4px 12px; }
.event-detail-grid > * { overflow-wrap: anywhere; }
/* The I/O strip is a bounded block at the header's right edge: chips wrap
   into rows inside it and tighten as the count grows, and the name chips give
   way (shrink, truncate) before the strip ever drops under them. */
.readout-panel { max-width: 480px; }
.readout-header > .q-chip { flex: 0 1 auto; min-width: 0; }
.readout-header .q-chip__content { min-width: 0; flex-wrap: nowrap; }
.readout-header .robot-face { flex-shrink: 0; }
.readout-robot-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.io-chips { flex: 0 0 auto; max-width: 200px; display: flex; flex-wrap: wrap; justify-content: flex-end; align-content: center; gap: 2px 1px; }
.io-chips .q-chip { margin: 0; }
.io-chips-dense .q-chip { padding: 0 3px; height: 1.4em; }
@media (min-width: 641px) and (max-width: 1200px) {
  .readout-panel { width: 450px; }
  .readout-panel > .nicegui-column { width: 100%; }
}
""")
