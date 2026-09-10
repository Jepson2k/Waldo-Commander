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
.plugin-panel-content { height: 100%; min-height: 0; min-width: 0; overflow: auto; overflow-x: hidden; }
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
.diagnostics-view > .panel-body { flex-basis: auto; }
.editor-toolbar-menu { min-width: 190px; }
.editor-toolbar-menu .q-item { min-height: 36px; }
.event-detail-grid { display: grid; grid-template-columns: minmax(90px, 1fr) minmax(0, 3fr); gap: 4px 12px; }
.event-detail-grid > * { overflow-wrap: anywhere; }
@media (min-width: 641px) and (max-width: 1200px) {
  .readout-panel { width: 450px; }
  .readout-panel > .nicegui-column { width: 100%; }
  .readout-header { flex-wrap: wrap !important; row-gap: 0; }
}
""")
