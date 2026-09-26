Waldo Commander is a web control panel for desktop robot arms. The whole window is a live 3D view of the robot, and every control floats over it on a glass panel. The system has two jobs: keep the controls readable whatever the scene is doing behind them, and make every colour mean exactly one thing, because an operator reads colour before they read words.

Dark is the only theme the app ships today: startup forces it and the theme selector in Settings is disabled. Every token still carries a light value, checked to the same standard, so the light mode that Settings promises can be switched on without a second design pass. What that switch needs is listed under "Theme lifecycle" below.

Every contrast figure in this system is a worst case: text on glass is checked over the empty scene, the live arm, the simulated arm and a white or black object behind the panel, at both ends of the glass gradient. Lines and markers in the scene are checked against the scene background.

## Colour carries one meaning

| Colour | Tokens | Means | Never used for |
|---|---|---|---|
| Amber | `mode-sim`, `scene-arm-sim` | The simulator is driving the arm. | Warnings, speed, drafts, tips, playback speed, chart series |
| No colour (grey) | `scene-arm`, `control` | Live hardware. Normal operation is the unmarked state. | |
| Sky | `action`, `action-text`, `progress`, `focus-ring` | The primary action; a control that is on or selected; the filled part of a level. | Status |
| Yellow | `warning`, `warning-fill`, `warning-soft`, `path-timing-warning` | Caution: freedrive active, first hardware move of an AI session, timing problems, log warnings. | The simulator |
| Red, filled | `estop` | The E-Stop. The only filled red control. | Record, delete, errors |
| Red | `error`, `record`, `scene-collision`, `path-invalid`, `physics-diverged`, `physics-contact` | Failure, disconnection, the recording dot, collisions, unreachable poses, tracking error, contact force. | Buttons |
| Emerald | `positive`, `run`, `physics-on-track` | Connected, succeeded; the Run and Play buttons; the simulated arm on its path. | Selection |
| Red / green / blue | `axis-*` | The X, Y and Z axes, and rotation about them. | Status |
| Teal | `scene-tool`, `path-tool-action`, `measure-position` | The attached tool, its actions, and its position trace. | |
| Magenta | `measure-current` | The tool's motor-current trace. | Anything outside a chart |
| Emerald / sky / violet | `ai-*` | The AI control mode, inside the top-centre capsule and its perimeter glow only. | Anything outside the capsule |
| Quasar fills | `fill-positive`, `fill-warning`, `fill-error` | What Quasar paints when it reaches for `positive`, `warning` or `negative` itself. | Hand-placed colour |

- Mark the simulator, never live hardware. When nothing is amber, the arm on screen is the real one.
- Pair every status colour with an icon or a word. In light theme `warning` sits close to `mode-sim` in hue; the warning icon is what tells them apart.
- Show `mode-sim` as a fill with `on-bright` on it. Never set amber text.
- Keep `ai-*` accents inside the capsule. Dialogs opened by the AI (approval, consent) are ordinary glass dialogs.
- Two fills sit below the 3:1 guideline against glass and are recognised by other means: the E-Stop disc (1.8:1 in dark over a white object) by its icon, size, shape and fixed position, and the simulator toggle (2.2:1) by its icon and label. Don't add a third.

## Surfaces

- Build every floating surface as glass: a 135° gradient from `glass` to `glass-end`, `backdrop-filter: blur(glass-blur) saturate(glass-saturate)`, a 1px `glass-border`, `shadow-glass`, `radius-md`. That covers panels, dialogs, tab rails, the playback bar and the AI capsule. Don't hand-roll a second recipe.
- Keep glass at 86% opacity. That floor keeps `text` at 6.4:1 or better and `text-muted` at 4.7:1 or better over any part of the scene, the arm included. The arm still shows through as a blurred glow.
- Put recessed content in a `well` with `radius-sm`: the log, text fields, expansion headers, the event log, the upload list. The pose readout and the code editor sit directly on the glass.
- Dim the scene with `scrim` behind a modal. Dialogs get no glow and no accent border. The confirming button is `action`, the dismissive one is a ghost button.
- Give nested surfaces `radius-sm`, never a radius larger than their container's.
- In light theme, `glass-border` is what separates a panel from the pale scene. Never drop it.

## Text and type

- Set text on glass, wells and `control` fills in `text`, and secondary text (descriptions, units, timestamps, hints, chart axis labels) in `text-muted`. `text-disabled` is only for the labels of disabled controls.
- On fills, use `on-bright` (for `action`, `run`, `mode-sim`, `warning-fill` and the AI accents) or `on-fill` (for `estop` and the `fill-*` Quasar fills). Never write literal white.
- Use the type styles by role:
  - `title`: panel and dialog titles such as Program, I/O and Log.
  - `headline`: one line per dialog at most.
  - `label`: setting names, tabs, buttons, joint names.
  - `body`: running text.
  - `caption`: descriptions, tooltips, chart axis labels.
  - `micro`: I/O chips and keyboard keys.
  - `code`: the editor and logs.
- Write sentence case everywhere. Quasar uppercases buttons and tabs by default; set `no-caps` so "Joint jog" never renders as "JOINT JOG".
- Set live values (`readout-lg`, `readout`, joint values) with `font-variant-numeric: tabular-nums` so digits don't shift while the arm moves.
- Always show the unit, right after the value, at `caption` size in the value's colour: `mm`, `°`, `°/s`, `mm/s`, `%`.

## Controls

- Filled buttons come in four colours: `action`, `run`, `estop` and `mode-sim`, plus `warning-fill` for caution confirmations. Every other button is a `control` fill or a ghost.
- Give a group at most one `action` fill. Home, camera reset, freedrive and gizmo buttons are `control` until they are on.
- In a toggle or segmented control, paint the selected segment `action` and the rest `control`. The I/O LOW/HIGH pair is a toggle: the current state is `action`, the other is `control`.
- The E-Stop is a round `estop` fill with an `on-fill` icon, and the only filled red circle anywhere. The record button is a `control` with a `record` dot icon in the record red: the round control carries the affordance, the dot the meaning.
- Freedrive active (brakes released) paints its button `warning-fill` with `on-bright`, because the arm can now fall or be pushed.
- Level indicators show their on state in `progress` against an off state in `control`. Those two are 3.6:1 apart in dark and 4:1 in light; `action` against `control` is only 1.3:1 in dark, so never use `action` for a level.
  - Joint bars: the track is `control`, the travelled portion `progress`, the ends `radius-pill`, the height `size-joint-bar`. The minus and plus caps are transparent over the bar. The joint angle and speed sit centred on the bar in `text` under a `scrim` halo, because the fill passes beneath them and `text` alone is 2:1 on `progress` in dark.
  - Speed and acceleration dots run a hue ramp: `level-speed-lo` to `level-speed-hi` (amber to red) and `level-accel-lo` to `level-accel-hi` (lime to teal), mixed in OKLCH per dot, with unselected dots on the same ramp at 40% opacity. The ramp is data, and the one place a warm hue is neither the simulator nor a fault.
  - The playback bar's played portion is `progress`.
- States: hover lays `state-hover` over the control, pressed lays `state-selected`, and keyboard focus draws a 2px `focus-ring` outline with a 2px offset. Disabled uses `opacity-disabled` with `text-disabled`. Locked out (the AI is driving, or another tab owns the session) uses `opacity-locked`, greyscale and no pointer events.
- Buttons are at least `size-control` in both directions. Inline controls in editor tabs use `size-control-sm`.

## Status and feedback

- Show feedback on the thing itself: tint the colliding link `scene-collision`, mark the unreachable line `error`, flip the chip. A toast is the last resort, for an event no surface on screen owns. When one is unavoidable, give it a Quasar semantic colour (`positive`, `warning`, `negative`, `info`), because those resolve to the `fill-*` tokens whose white text is already checked; never a hand-picked colour.
- Status badges are tinted: a `positive-soft`, `warning-soft` or `error-soft` background, text in the matching status colour, with an icon.
- The robot status chip is a status badge: `positive-soft` with `positive` text when connected to hardware, a `mode-sim` fill with `on-bright` text in simulator mode, and `error-soft` with `error` text when the robot is selected but disconnected. The help text that explains the chip shows these same tokens.
- Tool state dots: idle `info`, active and engaged `positive`, error `error`, nothing detected `text-muted`.
- Log lines: trace in `info`, debug in `text-muted`, info in `text`, warning in `warning`, error in `error`. Critical lines put `error` text on `error-soft`.
- Editor: the executing line is `editor-exec-line`. In a proposed diff, added lines sit on `positive-soft` and removed lines on `error-soft` with a strikethrough. New content flashes `positive` once, over `duration-flash`.
- Field validation messages are `error` text. Quasar would paint them `negative`; the theme restyles them.

## Axes

- Fill jog glyphs and scene markers with `axis-x`, `axis-y`, `axis-z` and their rotational tints `axis-rx`, `axis-ry`, `axis-rz`. Label glyphs in `on-bright`.
- Set axis text (readout labels, values and units) in `axis-*-text`, on the readout's glass. The X, Y and Z values are large text and hold 3.6:1 or better in dark over the scene and either arm; the rotation rows hold 4.7:1 or better. The fill colours fall to 2.3–4.1:1 as text, so never set text in `axis-x`, `axis-y` or `axis-z`.
- Rotation is always the lighter tint of its axis's hue: X is red, Y is green, Z is blue.

## Measurements

- Charts show series in `measure-position` (teal, like the tool) and `measure-current` (magenta, never amber). Lines, markers and the target and limit mark-lines take the series colour; axis labels and grid text are `text-muted`, and the legend names the series. The series colours hold 3.7:1 or better on glass as graphics; they are not text.
- The tool readout's position and current dots use the same two tokens.

## Scene and paths

- `scene-*`, `path-*` and `physics-*` tokens are resolved to hex, or to a 0–1 RGB triple for vertex colours, and handed to Three.js from the same dictionary that writes the CSS, because Three.js can't read CSS variables. They are checked against `scene-bg` at 3:1, so several differ per theme.
- The floor is an unlit `scene-ground` tint that dissolves into `scene-bg` by 1.3 times the reach, under a `scene-grid` polar grid whose lines fade out past the reach; the arm's shadow falls on a separate catcher. The scene is lit by a hemisphere light plus key, fill and rim lights, tone-mapped with ACES, and fogged to `scene-bg` in the distance. The meshes are the URDF's simplified visuals with a standard material and crease-aware smooth normals (32°), computed in the loader, so cylinders shade smoothly and machined edges stay sharp; the full-resolution files looked no better.
- Live arm `scene-arm`, simulated arm `scene-arm-sim`, editing `scene-arm-edit`. The tool is always teal (`scene-tool`, `scene-tool-moving`), whatever mode the arm is in.
- Keep-out shapes: confirmed program shapes `scene-shape`, unconfirmed ones `scene-shape-draft` (lighter, translucent), installation shapes `scene-shape-install`, proposed installation shapes `scene-shape-proposed`.
- Path preview: linear moves `path-cartesian`, joint moves `path-joints`, arcs and splines `path-smooth`, unreachable `path-invalid`, slower than the requested duration `path-timing-warning`. Invalid wins over timing when both apply. Timing colouring is not wired yet: the preview computes feasibility but the renderer colours by move type and validity only. The migration adds that mapping; until it lands, only the editor's line decoration shows a timing problem.
- Physics overlay: the achieved path is a vertex gradient from `physics-on-track` to `physics-diverged` by tracking error, contact-force arrows are `physics-contact`, and the centre of mass, its drop line and cone are `physics-com`. The physics legend's swatches read the same tokens, so the key and the scene can't drift apart.

## Space, shape, layers, motion

- Lay out with `gap`, not margins: `space-1` between an icon and its label, `space-2` between controls, `space-3` for panel padding and the panel inset from the window edge, `space-4` between dialog sections. Add no gap that isn't needed; the UI is dense on purpose.
- Three radii: `radius-sm` for controls and nested surfaces, `radius-md` for panels and dialogs, `radius-pill` for joint bars and their value pill, round buttons, the playback bar and the capsule.
- Stack app layers with the `z-*` tokens. Don't invent a z-index; if a new layer is needed, add a token.
- Motion: `duration-instant` for press feedback, `duration-fast` for hover and colour, `duration-base` with `ease-enter` for panels, `duration-flash` for new-content flashes, `duration-ambient` with `ease-loop` for the AI glow and the recording pulse. Only the Take-control button uses `ease-pop`. The robot face breathes on its own 6 to 8 second loops.
- Under `prefers-reduced-motion`, stop every loop and flash (AI glow, Take-control pulse, recording pulse, face breathing, tab and line flashes, panel slides) and keep only the press feedback.

## Voice

- Name things by what the operator sees: joints by name (Base, Shoulder, Elbow, Wrist 1–3), frames by what they are (World, Tool), the product as Waldo Commander.
- Label buttons with the verb that happens: "Take control", "Allow", "Run", "Save". Put the shortcut in the tooltip ("E-Stop (Esc)").
- Hints are short and imperative: "Hold a jog key for continuous movement."
- An error says what is wrong and how to fix it: "No camera active. Enable a tool camera in Settings."
- Show keyboard keys as `micro` keycaps, joined with a muted plus.

## Iconography

- Use Material Icons (filled), which NiceGUI bundles, through `ui.icon("name")` or a button's `icon=`. Icons take the colour of the text they sit with.
- The app's own glyphs live in `static/icons` and load with `ui.icon("img:/static/icons/<file>.svg")`. The cartesian jog arrows are single-ink SVGs filled with `currentColor` and coloured by their axis token. The robot faces (happy, neutral, sad) show connection state inside the status chip.
- No emoji.

## In the codebase

Every token lives once, in `waldo_commander/common/tokens.py` (generated from this system's tokens.json), and `theme.py` hands it to each consumer through one adapter per consumer:

| Consumer | How it reads a token | Example |
|---|---|---|
| CSS classes in `theme.py` | `--wc-<name>` custom property; dark on `:root`, light on `body.body--light` | `color: var(--wc-text-muted)` |
| Quasar `color=` / `text-color=` props | A registered Quasar colour named `wc-<name>`, from one `ui.colors(**{...})` call at startup, which also creates `bg-wc-<name>` and `text-wc-<name>` | `.props("color=wc-control")` |
| Quasar's own semantic names | `ui.colors(primary=action, secondary=action-hover, accent=action-text, info=action, positive=fill-positive, warning=fill-warning, negative=fill-error)` | a toast's `color="warning"` |
| Three.js (background, materials, lines) | The token resolved to a hex string, for the current theme | `hex_of("scene-arm")` |
| Three.js vertex colours | The token resolved to a 0–1 RGB triple | `rgb01("physics-on-track")` |
| ECharts options | The token resolved to hex, because the chart is a canvas and can't read CSS variables; options are rebuilt on a theme change | `"color": hex_of("measure-current")` |

- `var(--wc-...)` is a CSS value. It works in a class or a `style`, never as a Quasar `color=` prop, which expects a colour name. A dynamic state change such as `btn.props("color=...")` names a `wc-*` colour.
- A filled `wc-*` colour on a button, chip, badge or FAB always names its text token in the same props string: `color=wc-run text-color=wc-on-bright`. Quasar paints white on any fill through an `!important` rule inside a cascade layer, which outranks every app stylesheet, so there is no CSS-side pairing; `scripts/check_colors.py` refuses an unpaired fill and a browser test asserts the computed colours. A `ui.notification(type="warning")` passes `textColor="wc-on-fill"` for the same reason.
- Never write Quasar's semantic names (`primary`, `positive`, `negative`, `warning`, `info`) or palette names (`grey-7`, `teal-6`, `amber-8`) by hand. The semantic names exist so Quasar's own surfaces (notifications, badges, validation) come out pre-paired with white text.
- Outside `theme.py`: no hex, `rgb()` or `rgba()` literals; no Tailwind colour classes or `var(--color-*)`; no `color=white`; no `np.array` colour triples. `scripts/check_colors.py` runs as a pre-commit hook and enforces the textual ones; the numeric-triple rule is enforced by review of `services/urdf_scene`.
- No inline `.style()` for colour, radius, font size or shadow. Add a class next to the tokens in `theme.py` and use that.

### Theme lifecycle

Today `apply_theme("dark")` runs unconditionally at startup, the theme select is disabled, and "System" resolves to dark. Emitting `body.body--light` variables makes the light values available but does not switch anything on. Enabling light mode is a runtime change with these parts, and none of them is optional:

1. Resolve the effective theme at startup from the stored preference, and for System from `prefers-color-scheme`, with a listener for OS changes.
2. Toggle `body--light` and Quasar dark mode together.
3. Re-issue the Three.js background, ground, arm, tool, path and overlay colours from the resolved hex table; today they are chosen once when the scene is created.
4. Re-theme CodeMirror (`basicLight` / `oneDark`) and rebuild chart options from the resolved hex table.
5. Enable the theme select in Settings.

Until all five land, the light theme is a spec, not a feature, and the selector stays disabled.

### Migration order

1. `tokens.py` and `theme.py`: the generated token module, the `--wc-*` emit, the `ui.colors()` registration (`wc-<name>` Quasar colours, semantic names mapped), the `hex_of` / `rgb01` adapters, the type-style classes `wc-<style>`, the pre-commit check.
2. Panels and dialogs onto the glass recipe and `well`.
3. Controls: the action row, toggles, E-Stop, record, joint bars, the speed and acceleration ramps.
4. Status: chip, badges, log levels, editor decorations, tool state dots, validation text.
5. Scene: path colours per theme, the timing-feasibility mapping, physics overlay and legend, gripper chart.
6. Verify in both themes with objects behind the panels: consent dialog, latched E-Stop, recording, editor diff, AI capsule in each mode, telemetry.
