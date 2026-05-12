# CLAUDE.md - Project Guidelines for Claude Code

## Project Overview

Waldo Commander is a NiceGUI-based web interface for controlling PAROL6 robotic arms. It provides real-time robot control, script editing, motion recording, and 3D visualization.

## Testing Guidelines

### Browser Tests vs Simulated Tests

- **Use `user` fixture** (simulated) for tests that don't need real browser/JavaScript behavior - much faster
- **Use `screen` fixture** (real browser) only when testing actual browser behavior (JS execution, CSS rendering, etc.)

### Prefer Explicit Waits Over Fixed Sleeps

**Bad:**
```python
await asyncio.sleep(0.5)  # Wastes time if element appears sooner, fails if it takes longer
```

**Good:**
```python
# Wait for specific condition
await user.should_see(marker="some-element")

# Or with Selenium
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
WebDriverWait(driver, timeout=5).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".my-element")))
```

Fixed sleeps should only be used when:
- Waiting for animations (though animations are disabled in tests)
- Debouncing user input simulation
- No other condition can be checked

**For `user` fixture tests:** Sometimes a yield is needed between a click and an assertion. Start with `await asyncio.sleep(0)` first—this yields control without actually waiting. Only increase the sleep duration if the test still fails.

### Running Browser Tests

```bash
# Run all tests (headless by default)
pytest tests/

# Run with visible browser for debugging
HEADED=1 pytest tests/test_specific.py

# Run specific test file
pytest tests/test_editor_integration.py -v
```

### Test Environment Configuration

The following are **already configured in `conftest.py`** - do not set them manually:
- Headless browser mode is the default (only set `HEADED=1` to see the browser)
- `PAROL6_STATUS_RATE_HZ=20` (vs 50Hz default) - reduces CI load
- `PAROL6_FAKE_SERIAL=1` - uses simulator instead of hardware

**IMPORTANT: Do NOT prefix `pytest` commands with environment variables like `PAROL6_FAKE_SERIAL=1 pytest ...`. Everything is already set in conftest.py. Just run `pytest` directly.**

### CI Red Herring Errors

When debugging CI failures, this error is a **secondary symptom** that occurs after a primary failure:

- **`'AppConfig' object has no attribute 'binding_refresh_interval'`** - NiceGUI's binding loop tries to run after app teardown has started. Look at the logs **before** this error to find the root cause.

### Test Markers

- `@pytest.mark.integration` - Integration tests requiring full app setup
- `@pytest.mark.browser` - Tests requiring real browser (Selenium)
- `@pytest.mark.slow` - Long-running tests

### Testing Philosophy

**When CI tests fail, fix them.** Don't waste time analyzing whether failures are "related to your changes" — just fix all failing tests. The goal is a green CI, not attribution.

Prefer fewer, comprehensive integration tests that mimic manual testing over a large number of unit tests. We have no code coverage requirements—the goal is working features, not metrics.

**Test type selection:**

| Type | Value | Maintenance | When to use |
|------|-------|-------------|-------------|
| Integration (`user` fixture) | High | Low | Default choice for most feature testing |
| Browser (`screen` fixture) | High | High | Only when JS behavior must be tested |
| Unit tests | Low | Low | Isolating and testing backend logic |

**Guidelines:**

- **`user` fixture** is always preferred over `screen` for speed and simplicity
- **`screen` fixture** tests are brittle and hard to get right, but they're the closest thing to real testing—use only when necessary (JS-dependent features)
- **Unit tests** are sometimes necessary to isolate backend logic, but rarely preferred over integration tests
- Avoid testing "bloat"—more test code means more maintenance burden without proportional value
- A single comprehensive test that exercises a complete workflow is better than many shallow tests
- **Merge into one function** - When tests are variations of the same thing (e.g., positive/negative jog), combine into one test with multiple assertions
- **Class-level fixture sharing** - When tests are logically separate but don't need isolation, group them in a class with class-scoped fixtures to avoid per-test startup/teardown (especially important for expensive browser tests)
- **Test results are in `test-results.xml` — ALWAYS read this file after running tests.** Pytest writes JUnit XML to `test-results.xml` automatically. It contains test names, durations, failure messages, and full tracebacks. **Do NOT re-run tests** just to capture output you missed — the XML file already has everything. Do NOT grep/tail pytest console output; read the XML file instead.
- **NEVER run parol6 and web commander test suites in parallel** — no proper isolation, they share resources and have timing issues when resource-constrained. Always run sequentially.
- **NEVER allow subagents to run tests.** Many tests are timing-sensitive and the system doesn't have enough resources for agents and tests to run simultaneously. Only the main conversation should run tests, and only after all agents have completed.

## Code Patterns

### State Management

Global state is managed through dataclasses in `waldo_commander/state.py`:
- `robot_state` - Robot joint angles, position, I/O status
- `simulation_state` - Path visualization, targets, playback
- `ui_state` - UI component references
- `recording_state` - Motion recording mode

### NiceGUI Components

Custom components are in `waldo_commander/components/`:
- `editor.py` - Code editor with tabs, script execution
- `control.py` - Jogging controls, robot mode switching
- `readout.py` - Joint/position readouts

### Services

Background services in `waldo_commander/services/`:
- `script_runner.py` - Python script subprocess management
- `path_visualizer.py` - Motion path simulation
- `motion_recorder.py` - Recording robot movements to code

## Common Tasks

### Adding a New UI Element

1. Add to appropriate component in `waldo_commander/components/`
2. Add marker with `.mark("descriptive-marker-name")` for testing
3. Add test in `tests/test_*_integration.py`

### Modifying Robot Communication

Robot communication goes through a `waldoctl.RobotClient` ABC. Each backend (e.g. `parol6`) provides async and sync client implementations. The client is created via `robot.create_async_client()` in `main.py` and passed to components that need it.

## Code Style

- **Comments**: Describe the final implementation, not what changed. Avoid "changed X to Y" or "added this because..." comments.
- **Tests**: Use deterministic waits (polling for conditions) rather than blind sleeps. Exception: very small sleeps (~0.1s) for debouncing are acceptable.
- **Exception handling**: Never use `except Exception: pass`. Either catch specific exceptions with `pass`, or if catching broad exceptions, log or handle the error meaningfully.

### UI Component Preferences
- **Prefer NiceGUI native elements** (ui.chip, ui.image, ui.icon, ui.label, ui.row, etc.) over raw HTML (`ui.html`). Only use `ui.html` when NiceGUI doesn't provide an equivalent.
- **Use `ui.icon`** for Material Icons. Use `ui.icon("img:path")` for custom SVGs. Don't inline SVG content in Python strings.
- **Keep layouts compact** — avoid unnecessary gaps. Don't add `gap-*` classes unless spacing is actually needed.
- **Color preference order**: Tailwind or Quasar color classes first, then `oklab()`, then raw hex as last resort.

## Multi-Repo Versioning & Dev Workflow

### Repository structure

Three repos with a clear dependency direction:

```
waldoctl (ABC/types) ← parol6 (backend) ← waldo-commander (frontend)
```

- `waldoctl` — shared interface definitions, installed from git tag in pyproject.toml
- `parol6` — PAROL6 backend, installed from git tag by CI
- `waldo-commander` — the frontend, depends on both via git URLs

WC also depends on a vendored fork of NiceGUI; see [NiceGUI fork](#nicegui-fork) below.

### Versioning

All packages use semver (`MAJOR.MINOR.PATCH`). Pre-1.0 packages bump minor for breaking changes. Version is set in each package's `pyproject.toml` and read at runtime via `importlib.metadata`.

### Release workflow (breaking cross-repo changes)

1. Release waldoctl first — bump version in pyproject.toml, merge to main, tag (e.g. `v0.2.0`)
2. Update parol6's `pyproject.toml` to reference the new waldoctl tag, merge, tag
3. Update waldo-commander's `pyproject.toml` + CI to reference new tags, merge, tag

### Dev workflow (coordinated feature branches)

Use the **same branch name** across all repos that have changes. CI automatically detects matching branches:

- If waldoctl has a branch matching the PR branch name, CI installs from that branch
- If parol6 has a matching branch, CI installs from that branch
- Otherwise, CI falls back to the tagged version in pyproject.toml

This means devs can work on cross-repo changes without publishing releases first. The branch-matching logic is in `.github/workflows/tests.yml`.

### Local development

For the common case — editing `waldo_commander/` only, with `waldoctl` and
`parol6` pulled from the pinned git tags:

```bash
pip install -e ".[dev]"
```

This is what most contributors should use. It installs `waldoctl`, `parol6`,
and the vendored `nicegui` fork from the git URLs pinned in `pyproject.toml`,
plus all dev tools (ruff, ty, pytest, pre-commit, selenium).

#### Advanced: editing sibling repos in place

If you also need to edit `waldoctl/` or `PAROL6-python-API/` directly, clone
them as sibling directories and use this multi-step install:

```bash
pip install -e waldoctl/
pip install -e PAROL6-python-API/ --config-settings editable_mode=compat --no-deps
pip install -e . --no-deps
pip install -e ".[dev]"   # final pass picks up the rest of the deps
```

Two flags are load-bearing here, for two unrelated reasons:

**`--no-deps`** is needed because both `Waldo-Commander/pyproject.toml` and
`PAROL6-python-API/pyproject.toml` pin `waldoctl @ git+...@v0.2.0` as a
*direct URL* requirement, and pip's resolver refuses to reconcile a direct
URL spec with a local editable install of the same version — even when both
resolve to identical code. Empirically verified: `pip install -e waldoctl/
-e PAROL6-python-API/ -e ".[dev]"` fails with `ResolutionImpossible` on
pip 25, saying "waldoctl 0.2.0 (from local path)" conflicts with "waldoctl
0.2.0 (from git URL)". Installing the editable sibling first and skipping
dep resolution on the dependents bypasses the conflict.

**`--config-settings editable_mode=compat`** is needed only for parol6,
because `parol6/robot.py` resolves the URDF via
`importlib.resources.files("parol6")`. Pip's default PEP 660 "strict"
editable install puts a thin shim directory in site-packages and that's
what `files("parol6")` returns — so `pkg_files("parol6") /
"urdf_model/urdf/PAROL6.urdf"` resolves under site-packages instead of
the source tree, and `waldo-commander` crashes at startup with
`File ...site-packages/parol6/urdf_model/urdf/PAROL6.urdf does not exist`.
The `compat` mode falls back to the older `.pth`/develop-style install
where `files("parol6")` correctly returns the source tree path. waldoctl
and waldo_commander don't need this flag (waldoctl has no package data,
and waldo_commander happens to resolve correctly even in strict mode).

### NiceGUI fork

WC pins NiceGUI to a SHA on `Jepson2k/nicegui`'s `combo-all-prs` branch — a synthetic branch that merges all of our open upstream PRs together so WC can use them as one unit. SHA pin (not branch ref) for reproducibility.

When an underlying PR branch changes, combo goes stale. Refresh it with `/nicegui-combo-bump`, which rebuilds combo and re-pins WC's `pyproject.toml`.

**Never commit directly to `combo-all-prs`** — it's regenerated each cycle and direct commits get clobbered. Anything that needs to persist must land on a feature branch first.
