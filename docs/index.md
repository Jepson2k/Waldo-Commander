# Waldo Commander

A web interface for controlling robotic arms, currently tested with the [PAROL6](https://github.com/PCrnjak/PAROL6-Desktop-robot-arm) robot.

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/demo_showcase.mp4" type="video/mp4">
</video>

- **Browser-based.** Control from any device on the network without being tethered to the arm.
- **Python programs.** Write robot programs in Python with loops, math, and libraries. Built-in editor with auto-complete, live output, and step-through debugging.
- **3D simulation.** Preview motion paths, check reachability, and scrub through the timeline — no physical robot needed.
- **Teach by demonstration.** Control the robot live and record the motions as Python code.
- **Backend-agnostic.** Robot-specific logic lives behind the [waldoctl](https://github.com/Jepson2k/waldoctl) abstraction layer. Other robots can be integrated by implementing the same interfaces — see the [Backend Development Guide](guides/backend-development.md).

---

## Getting Started

Requires Python 3.12+. Runs on Linux, macOS, and Windows.

```bash
git clone https://github.com/Jepson2k/Waldo-Commander.git
cd Waldo-Commander
pip install -e ".[parol6]"
waldo-commander
```

Open the printed URL. No robot connected? The app auto-starts in simulator mode.

### Basic Controls

Jog in joint space (one joint at a time) or Cartesian space (translate in XYZ, rotate around RX/RY/RZ). Cartesian translation currently operates in the World reference frame while cartesian rotation operates in Tool reference frame. Future support is planned for additional reference frames.

Keyboard shortcuts: **WASD** + **Q/E** for Cartesian movement, **[/]** to adjust speed. Clicking a jog button or key sends a single step; holding it jogs continuously until you release.

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/basic_control.mp4" type="video/mp4">
</video>

### Connecting Your Robot

In the control panel, switch to the **Settings** tab and select your hardware connection. On Linux you'll need access to the serial device — add yourself to the `dialout` group or set up a udev rule. Connection status is shown in the top right corner.

- <span style="color: #4caf50">&#9632;</span> Connected to robot hardware
- <span style="color: #f44336">&#9632;</span> Robot mode but disconnected
- <span style="color: #9e9e9e">&#9632;</span> Simulator mode

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/connecting_to_robot.mp4" type="video/mp4">
</video>

### Programming, Recording, and Path Visualization

Write robot programs in Python using the built-in editor with auto-complete for all robot commands. Or jog the robot into position and let the recorder generate `move_j` / `move_l` calls for you — I/O and tool actions are captured too. Right-click in the 3D view to place targets, press **T** to add one at the current pose, or drag existing targets with the gizmo to reposition them.

Run programs against the simulator to preview the motion path in 3D. The path traces the TCP position through each move, color-coded by reachability. Execute on hardware when you're ready.

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/recording_and_previewing_actions.mp4" type="video/mp4">
</video>

### I/O and Tool Control

Toggle digital outputs, read inputs, and monitor E-stop state. For grippers, slide the position and current controls and watch the gripper track in real time — a live chart plots position and current over time. Tool and variant switching happens in the Settings tab; the 3D model updates to show the attached tool.

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/attaching_a_tool.mp4" type="video/mp4">
</video>

### Camera Feed

An MJPEG camera stream can be displayed in the gripper panel — useful for monitoring pick-and-place or running ML inference on the end-effector view. On Linux, frames pass straight from the kernel to the browser via v4l2 with zero re-encoding. Virtual camera devices work too — pipe a CV pipeline through `pyvirtualcam` and display the annotated feed.

### Hand-Eye Calibration

For a camera mounted on the tool, the **Hand-Eye Calibration** tab solves the camera→TCP transform (eye-in-hand) so camera observations can be mapped into robot coordinates. Download the generated ChArUco board, print it at 100% scale, and fix it in the workspace — tilted roughly 30-45° toward the camera rather than square-on, which conditions the solve much better. Then jog the robot so the camera sees the board from 10–15 poses with varied wrist orientation, capturing a sample at each; detected corners are overlaid on the live feed and a capture is only accepted while the board is detected and the robot is stationary. Solving estimates the camera intrinsics from the same captures and runs `cv2.calibrateHandEye`, reporting reprojection error, AX=XB residuals, and target spread. Saved calibrations are stored per tool and can be visualized as a camera frustum in the 3D scene.

---

## Configuration

### CLI Options

```bash
waldo-commander [options]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--host HOST` | Webserver bind host | `0.0.0.0` |
| `--port PORT` | Webserver bind port | `8080` |
| `--controller-host HOST` | Controller host to connect to | `127.0.0.1` |
| `--controller-port PORT` | Controller port | `5001` |
| `--log-level LEVEL` | Set log level (TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL) | `WARNING` |
| `-v`, `-vv` | Increase verbosity (INFO / DEBUG) | |
| `-q` | Enable WARNING logging | |
| `--reload` | Auto-reload on file changes (dev mode) | |

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `WALDO_CONTROLLER_IP` | Controller host | `127.0.0.1` |
| `WALDO_CONTROLLER_PORT` | Controller port | `5001` |
| `WALDO_SERVER_IP` | NiceGUI bind host | `0.0.0.0` |
| `WALDO_SERVER_PORT` | NiceGUI HTTP port | `8080` |
| `WALDO_AUTO_START` | Auto-start the backend controller on app launch | `1` |
| `WALDO_LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) | `WARNING` |
| `WALDO_WEBAPP_CONTROL_RATE_HZ` | Jog command emission rate from the UI | `20` |
| `WALDO_WEBAPP_AUTO_SIMULATOR` | Auto-enable simulator when no hardware connection is configured | `1` |
| `WALDO_WEBAPP_REQUIRE_READY` | Wait for backend ready and enable status streaming on startup | `1` |
| `WALDO_TRACE` | Enable TRACE-level logging in console and UI logs | off |
| `WALDO_EXCLUSIVE_START` | Require exclusive controller ownership on start | `1` |

### Settings Panel

The **Settings** tab in the control panel provides:

- **Hardware connection** — auto-detects available ports, or enter a path manually. Refreshes every 10 seconds. Persisted in browser local storage.
- **Theme** — currently dark only. Light mode is planned for a future update.
- **Motion profile** — selects the trajectory planner used for planned motions. Available profiles depend on the backend. See the [PAROL6 motion profiles](https://github.com/Jepson2k/PAROL6-python-API#motion-profiles) documentation for the profiles available with the default backend.
- **Workspace envelope** — an approximate visualization of the robot's reachable space. Computed by running FK on a grid of ~500k joint configurations and taking the convex hull of the resulting TCP positions. This gives an outer boundary — not every point inside the hull is necessarily reachable.
    - **Auto** — shows a clipped section of the envelope only when the TCP approaches the boundary (within 100mm). Gives you a heads-up without cluttering the view.
    - **On** — always visible as a full translucent shell.
    - **Off** — hidden.
- **Camera** — select a video device for the gripper panel feed, often used for monitoring pick-and-place or running ML inference on the end-effector view. If you'd like to add annotations to the camera feed, you can do so by processing the raw webcam in your own script and outputting to a virtual camera via pyvirtualcam + v4l2loopback — then just select that virtual device here. On Linux: `sudo apt install v4l2loopback-dkms`.
- **Tool** — select the active end-effector from the tools the backend provides. See the [PAROL6 tools](https://github.com/Jepson2k/PAROL6-python-API#tools) documentation for the tools available with the default backend. Changing the tool updates the TCP offset for Cartesian calculations, swaps the tool mesh in the 3D view, and re-runs any active simulation. If a tool has variants (e.g. different jaw sets), a variant selector appears. A per-tool TCP offset field lets you fine-tune the tool tip position in mm.

### Running on a Remote Machine

If your backend controller runs on a different machine than the web UI:

1. Start the controller on the machine connected to the robot.
2. Ensure the controller's port is accessible from the UI machine.
3. Set `WALDO_CONTROLLER_IP` and `WALDO_CONTROLLER_PORT` on the UI machine.

For lowest latency, run the web UI and controller on the same machine.

---

## Contributing

```bash
pip install -e ".[dev]"
pre-commit install
```

Pre-commit hooks run ruff and ty on every commit. Tests use `pytest`.

### Editing sibling repos in place

If you need to edit `waldoctl/` or `PAROL6-python-API/` alongside `waldo_commander/`, clone them as sibling directories and run:

```bash
pip install -e waldoctl/
pip install -e PAROL6-python-API/ --config-settings editable_mode=compat --no-deps
pip install -e . --no-deps
pip install -e ".[dev]"
```

`--no-deps` sidesteps a pip resolver conflict between the local editable `waldoctl` and the `waldoctl @ git+...` direct URL pin in dependents. `editable_mode=compat` is needed only for parol6, so `importlib.resources.files("parol6")` resolves to the source tree (otherwise the URDF lookup fails at startup).

---

## About

Waldo Commander builds on the open-source PAROL6 robotics ecosystem:

- [PAROL6 Desktop Robot Arm](https://github.com/PCrnjak/PAROL6-Desktop-robot-arm) — hardware designs and BOM by Source Robotics
- [PAROL6 Python API](https://github.com/Jepson2k/PAROL6-python-API) — headless controller and UDP client
- [waldoctl](https://github.com/Jepson2k/waldoctl) — robot backend abstraction layer
- [pinokin](https://github.com/Jepson2k/pinokin) — Pinocchio-based FK/IK bindings

The web interface is built with [NiceGUI](https://nicegui.io/).

The name "Waldo" comes from Robert Heinlein's 1942 story *Waldo*, about remote manipulator devices — the concept that inspired the real-world term "waldo" for teleoperated arms.

### License

See the [LICENSE](https://github.com/Jepson2k/Waldo-Commander/blob/main/LICENSE) file.
