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
  /* A skill's form opens where the side panels do, clear of the scene, the
     readout and the E-stop. */
  .skill-dialog-host > .q-dialog__inner--left { align-items: flex-start !important; padding: 12px 0 12px 58px !important; }
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
/* A flex column with the tab panel's full width (the tab panel starts its
   children, so without a width a plugin's content shrinks to its widest
   line), so a panel capped at the viewport shrinks its plugin's own scroller
   rather than scrolling the plugin whole, heading and all. */
.plugin-panel-content { display: flex; flex-direction: column; flex: 1 1 auto; width: 100%; height: 100%; min-height: 0; min-width: 0; overflow: auto; overflow-x: hidden; }
.panel-body { flex: 1 1 0; min-height: 0; min-width: 0; width: 100%; overflow-y: auto; overflow-x: hidden; }
.panel-heading { font-size: 16px; font-weight: 600; line-height: 24px; }
.panel-actions { flex-shrink: 0; width: 100%; align-items: center; gap: 8px; padding-top: 8px; }
.panel-note { color: var(--color-neutral-300); font-size: 12px; line-height: 1.45; }
.task-dialog { max-height: calc(100dvh - 32px); padding: 16px; gap: 12px; }
.task-dialog .q-card__section { min-width: 0; }
.task-dialog > .panel-body { flex-basis: auto; }
.task-panel .q-table th, .task-dialog .q-table th { color: var(--color-neutral-300); text-align: left; }
.task-panel .q-table td, .task-dialog .q-table td { text-align: left; }
.settings-content { flex: 1 1 auto; min-height: 0; width: 100%; gap: 0; flex-wrap: nowrap; overflow-y: auto; overflow-x: hidden; }
.settings-panel { max-width: calc(100vw - 80px); }
/* The rule under each heading is the only divider; a row is its label beside
   its control, and what the setting does is the label's tooltip. */
.settings-group-heading { width: 100%; margin-top: 12px; padding-bottom: 3px; border-bottom: 1px solid var(--color-neutral-700); font-size: 12px; font-weight: 600; color: var(--ctk-muted); }
.settings-content > .settings-group-heading:first-child { margin-top: 0; }
.settings-row { display: grid; grid-template-columns: 150px minmax(0, 1fr); column-gap: 8px; align-items: center; min-height: 29px; width: 100%; }
.settings-row > .settings-label { font-size: 13px; line-height: 1.25; min-width: 0; color: var(--color-neutral-200); }
.settings-row > :not(.settings-label) { justify-self: start; min-width: 0; max-width: 100%; }
.settings-row .q-field__control, .settings-row .q-field__marginal { min-height: 28px; height: 28px; }
.settings-row .q-field__native, .settings-row .q-field__prefix, .settings-row .q-field__suffix { min-height: 28px; padding-top: 0; padding-bottom: 0; }
.settings-row .q-toggle__inner { font-size: 28px; }
.settings-axis { width: 76px; }
.settings-axis .q-field__prefix { color: var(--ctk-muted); padding-right: 4px; }
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
