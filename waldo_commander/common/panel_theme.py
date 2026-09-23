"""Shared presentation for task panels and measurement charts."""

from nicegui import ui

JOINT_COLORS = ["#7dd3fc", "#86efac", "#fdba74", "#fda4af", "#c4b5fd", "#fde047"]
CHART_TEXT = "#d4d4d4"
CHART_GRID = "rgba(163,163,163,0.18)"


def inject_panel_css() -> None:
    ui.add_css("""
/* Quasar's important utility colors live in a layer; unlayered rules lose
   to them even at higher specificity. */
@layer quasar {
  .q-btn--flat.text-primary:not(.text-white),
  .q-btn--outline.text-primary:not(.text-white) { color: #7dd3fc !important; }
}
.task-panel, .overlay-card.task-panel, .task-dialog {
  color: var(--color-neutral-100);
  background: var(--color-neutral-800) !important;
  font-size: 14px;
}
.task-panel .q-btn, .task-dialog .q-btn { text-transform: none; }
.task-panel .q-tab, .task-dialog .q-tab { text-transform: none; min-height: 36px; }
.task-panel .q-field__label, .task-dialog .q-field__label { font-size: 16px; }
.task-panel .text-caption, .task-dialog .text-caption { font-size: 12px; line-height: 1.45; }
.task-panel .q-separator, .task-dialog .q-separator { background: var(--color-neutral-600); }
.task-panel .q-expansion-item > .q-expansion-item__container > .q-item,
.task-dialog .q-expansion-item > .q-expansion-item__container > .q-item {
  min-height: 36px; padding: 4px 8px; background: transparent;
}
/* The tab panel is a flex column that starts its children, so without a
   width a plugin's content shrinks to its widest line. */
.plugin-panel-content { width: 100%; height: 100%; min-height: 0; min-width: 0; overflow: auto; overflow-x: hidden; }
.panel-body { flex: 1 1 0; min-height: 0; min-width: 0; width: 100%; overflow-y: auto; overflow-x: hidden; }
.panel-heading { font-size: 16px; font-weight: 600; line-height: 24px; }
.panel-actions { flex-shrink: 0; width: 100%; align-items: center; gap: 8px; padding-top: 8px; }
.panel-note { color: var(--color-neutral-300); font-size: 12px; line-height: 1.45; }
.task-dialog { max-height: calc(100dvh - 32px); padding: 16px; gap: 12px; }
.task-dialog .q-card__section { min-width: 0; }
.task-dialog > .panel-body { flex-basis: auto; }
.task-panel .q-table th, .task-dialog .q-table th { color: var(--color-neutral-300); text-align: left; }
.task-panel .q-table td, .task-dialog .q-table td { text-align: left; }
.settings-content { height: 100%; min-height: 0; width: 100%; gap: 4px; flex-wrap: nowrap; }
.settings-group-heading { font-size: 13px; font-weight: 600; color: var(--ctk-muted); letter-spacing: .01em; }
.settings-panel { width: 520px; max-width: calc(100vw - 80px); }
.settings-category { flex-shrink: 0; width: 100%; }
.settings-category .q-field__control, .settings-category .q-field__marginal { height: 32px; min-height: 32px; }
.settings-category .q-field__native { min-height: 32px; padding: 0; }
.settings-group { width: 100%; gap: 4px; flex-wrap: nowrap; }
.settings-row { display: flex; flex-wrap: nowrap; gap: 8px; min-height: 32px; width: 100%; align-items: center; }
.settings-row > .settings-label { flex: 1 1 0; min-width: 0; font-size: 14px; }
.settings-row > :not(.settings-label) { flex-shrink: 0; max-width: 60%; }
.settings-row .q-field__control, .settings-row .q-field__marginal { min-height: 32px; height: 32px; }
.settings-row .q-field__native { min-height: 32px; padding-top: 0; padding-bottom: 0; }
.settings-row .q-field__label { top: 7px; }
.settings-row .q-field--float .q-field__label { transform: translateY(-35%) scale(.75); }
.settings-row .q-field--float .q-field__native { padding-top: 12px; }
.settings-row .q-toggle__inner { font-size: 32px; }
.settings-content .q-expansion-item { width: 100%; }
.settings-content .q-item { min-height: 32px; padding: 4px 0; background: transparent; }
.settings-content .q-item__section--avatar { min-width: 24px; }
.settings-content .q-separator { background: var(--color-neutral-600); }
.diagnostics-view { width: 560px; max-width: calc(100vw - 80px); max-height: calc(100dvh - 24px); flex-wrap: nowrap; }
/* Normal is colourless, so anything with colour in it is asking for attention.
   The verdict is the one thing sized to be read from across the room. */
.diag-verdict { font-size: 17px; font-weight: 600; color: var(--ctk-text); }
.diag-ok { color: var(--ctk-text); }
.diag-warn { color: var(--sem-warning); }
.diag-fault { color: var(--sem-danger); }
.diag-verdict.diag-ok { color: var(--ctk-text); }
/* The period budget drawn as its full width, so how close the loop runs to
   its deadline is a position rather than a number to be compared from memory. */
.diag-bar { flex: 0 0 auto; width: 180px; height: 4px; margin: 3px 0 5px; border-radius: 2px; background: rgba(163,163,163,0.18); overflow: hidden; }
.diag-bar-fill { height: 100%; width: 0; border-radius: 2px; background: var(--color-neutral-400); transition: width .2s linear; }
.diag-bar-fill.over { background: var(--sem-warning); }
.diagnostics-view > .panel-body { flex-basis: auto; }
/* Calibration is four steps: the ribbon is where the operator is, the
   thumbnails are what they have, and a view's border is the only colour. */
.handeye-steps { gap: 0; border-bottom: 1px solid var(--color-neutral-700); }
.handeye-step { flex: 1 1 0; border-radius: 0; border-bottom: 2px solid transparent; color: var(--ctk-muted) !important; }
.handeye-step.handeye-step-done { color: var(--ctk-text) !important; }
.handeye-step.handeye-step-active { color: var(--ctk-text) !important; border-bottom-color: #7dd3fc; }
.handeye-views { display: grid; grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); gap: 6px; }
.handeye-view { position: relative; aspect-ratio: 4 / 3; border: 1px solid var(--color-neutral-700); border-radius: 4px; overflow: hidden; background: var(--color-neutral-900); }
.handeye-view img { width: 100%; height: 100%; object-fit: cover; display: block; }
.handeye-view-index { position: absolute; left: 5px; top: 2px; font-size: 11px; color: var(--ctk-text); text-shadow: 0 0 3px #000; }
.handeye-view .q-btn { position: absolute; right: 0; top: 0; }
.handeye-view-similar { border-color: var(--sem-warning); }
.handeye-view-error { border-color: var(--sem-danger); }
.handeye-verdict { font-size: 17px; font-weight: 600; }
.handeye-camera-card { width: 100%; max-width: 360px; margin: 0 auto; }
/* Skills are drawn rather than listed: the diagram is the label. */
.skill-group-heading { font-size: 13px; font-weight: 600; color: var(--ctk-muted); margin-top: 4px; }
.skill-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(92px, 1fr)); gap: 6px; width: 100%; }
.skill-tile { min-height: 80px; padding: 6px 4px; border: 1px solid var(--color-neutral-700); border-radius: 6px; background: var(--color-neutral-900); color: var(--color-neutral-200); }
.skill-tile:hover, .skill-tile.skill-tile-selected { border-color: #7dd3fc; }
.skill-tile .q-icon { width: 56px; height: 40px; font-size: 40px; margin-bottom: 4px; }
.skill-tile .q-btn__content .block { font-size: 12px; line-height: 1.25; white-space: normal; }
.skill-tile-unavailable { opacity: .45; }
.skill-detail-icon { width: 42px; height: 30px; font-size: 30px; flex-shrink: 0; }
.editor-toolbar-menu { min-width: 190px; }
.editor-toolbar-menu .q-item { min-height: 36px; }
.event-detail-grid { display: grid; grid-template-columns: minmax(90px, 1fr) minmax(0, 3fr); gap: 4px 12px; }
.event-detail-grid > * { overflow-wrap: anywhere; }
/* The I/O strip is the readout's widest row on a backend that takes its line
   count from config. Bounded so it wraps instead of widening the panel, and
   given the header's full width on its own line once there are many lines. */
.readout-panel { max-width: 480px; }
.readout-header { flex-wrap: wrap; }
.io-chips { flex-wrap: wrap; row-gap: 2px; max-width: 240px; }
.io-chips-wide { flex: 1 0 100%; max-width: none; justify-content: flex-start; }
@media (min-width: 641px) and (max-width: 1200px) {
  .readout-panel { width: 450px; }
  .readout-panel > .nicegui-column { width: 100%; }
  .readout-header { flex-wrap: wrap !important; row-gap: 0; }
}
""")
