"""Inspectable screenshots and animation from real browser interactions."""

from contextlib import contextmanager
from pathlib import Path
from io import BytesIO
import json
import subprocess
import hashlib
from importlib.metadata import version, PackageNotFoundError
import base64
import threading
import time
import os
import sys
from shutil import which
from urllib.request import urlopen

from PIL import Image


@contextmanager
def preserve_capture_buffers(screen):
    """Keep rendered WebGL pixels available to Chrome's screenshot compositor.

    Only visual evidence uses this context. Performance measurements use the
    ordinary renderer; preserving a drawing buffer changes its GPU costs.
    """
    driver = screen.selenium
    registration = driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
          const getContext=HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext=function(kind, options) {
            if (kind==='webgl2' || kind==='webgl' || kind==='experimental-webgl')
              options={...options,preserveDrawingBuffer:true};
            return getContext.call(this,kind,options);
          };
        """
        },
    )
    try:
        driver.refresh()
        yield
    finally:
        driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument", registration)


class VisualEvidence:
    def __init__(self, screen, name: str, *, synchronized_frames=False):
        self.screen = screen
        self.root = Path(".scene-evidence") / name
        self.root.mkdir(parents=True, exist_ok=True)
        for pattern in (
            "*.png",
            "*.gif",
            "*.mp4",
            "manifest.json",
            "frames/*.png",
            "motion-frames/*.jpg",
        ):
            for previous in self.root.glob(pattern):
                previous.unlink()
        self.frames = []
        self.captures = []
        self.video_frames = []
        self.video_error = None
        self.synchronized_frames = synchronized_frames
        self.video_stop = threading.Event()
        screen.selenium.execute_script("""
        if (!window.__sceneEvidencePointer) {
          const pointer = document.createElement('div');
          pointer.style.cssText = 'position:fixed;width:12px;height:12px;border:2px solid var(--wc-action);border-radius:50%;pointer-events:none;z-index:10000;transform:translate(-50%,-50%)';
          document.body.append(pointer);
          window.addEventListener('pointermove', (event) => {pointer.style.left = event.clientX + 'px'; pointer.style.top = event.clientY + 'px';});
          window.__sceneEvidencePointer = pointer;
        }
        """)
        address = screen.selenium.capabilities.get("goog:chromeOptions", {}).get(
            "debuggerAddress"
        )
        self.video_thread = None
        if address:
            with urlopen(f"http://{address}/json", timeout=5) as response:
                pages = json.load(response)
            page = next(
                p
                for p in pages
                if p.get("type") == "page"
                and p.get("url") == screen.selenium.current_url
            )
            self.video_thread = threading.Thread(
                target=self._record_synchronized
                if synchronized_frames
                else self._record,
                args=(page["webSocketDebuggerUrl"],),
                daemon=True,
            )
            self.video_thread.start()

    def _record_synchronized(self, url):
        import websocket

        connection = None
        serial = 0
        try:
            connection = websocket.create_connection(
                url, timeout=5, suppress_origin=True
            )

            def request(method, params):
                nonlocal serial
                serial += 1
                connection.send(
                    json.dumps({"id": serial, "method": method, "params": params})
                )
                while True:
                    response = json.loads(connection.recv())
                    if response.get("id") != serial:
                        continue
                    if "error" in response:
                        raise ValueError(response["error"])
                    return response.get("result", {})

            deadline = time.monotonic() + 90
            while not self.video_stop.is_set() and time.monotonic() < deadline:
                started = time.monotonic()
                # Screencast events can read a discarded WebGL buffer on DOM-only frames.
                request(
                    "Runtime.evaluate",
                    {
                        "expression": """new Promise(resolve => {
                  const root = document.querySelector('.wc-scene');
                  const S = root && getElement(root)?.core?.surface;
                  if (!S) { resolve(false); return; }
                  const frame = S.frames(); S.requestRender();
                  const drawn = () => S.frames() > frame ? resolve(true) : requestAnimationFrame(drawn);
                  requestAnimationFrame(drawn);
                })""",
                        "awaitPromise": True,
                        "timeout": 4000,
                    },
                )
                result = request(
                    "Page.captureScreenshot",
                    {"format": "jpeg", "quality": 85, "fromSurface": True},
                )
                self.video_frames.append(
                    (time.time(), base64.b64decode(result["data"]))
                )
                self.video_stop.wait(max(0, 0.1 - (time.monotonic() - started)))
        except (OSError, ValueError, websocket.WebSocketException) as error:
            self.video_error = str(error)
        finally:
            if connection:
                connection.close()

    def _record(self, url):
        import websocket

        connection = None
        try:
            connection = websocket.create_connection(
                url, timeout=0.5, suppress_origin=True
            )
            connection.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Page.startScreencast",
                        "params": {
                            "format": "jpeg",
                            "quality": 85,
                            "maxWidth": 1920,
                            "maxHeight": 1080,
                            "everyNthFrame": 1,
                        },
                    }
                )
            )
            previous = 0.0
            serial = 1
            deadline = time.monotonic() + 90
            while not self.video_stop.is_set() and time.monotonic() < deadline:
                try:
                    message = json.loads(connection.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if message.get("method") != "Page.screencastFrame":
                    continue
                serial += 1
                params = message["params"]
                connection.send(
                    json.dumps(
                        {
                            "id": serial,
                            "method": "Page.screencastFrameAck",
                            "params": {"sessionId": params["sessionId"]},
                        }
                    )
                )
                now = params["metadata"].get("timestamp", time.time())
                if now - previous >= 0.08:
                    self.video_frames.append((now, base64.b64decode(params["data"])))
                    previous = now
        except (OSError, ValueError, websocket.WebSocketException) as error:
            self.video_error = str(error)
        finally:
            if connection:
                connection.close()

    def capture(self, name: str) -> None:
        import waldoctl
        from waldo_commander.state import ui_state

        data = self.screen.selenium.get_screenshot_as_png()
        (self.root / f"{name}.png").write_bytes(data)
        self.frames.append(Image.open(BytesIO(data)).convert("RGB"))
        self.captures.append(
            {
                "name": name,
                "viewport": self.frames[-1].size,
                "robot_appearance": ui_state.urdf_scene._appearance_mode.value,
                "controller_mode": "simulator"
                if waldoctl.commander.status.simulator_active
                else "robot",
                **self.screen.selenium.execute_script(
                    "return {theme: document.body.classList.contains('body--dark') ? 'dark' : 'light', touch_points: navigator.maxTouchPoints}"
                ),
            }
        )

    def finish(self) -> None:
        self.stop()
        assert self.video_error is None, (
            f"Browser video recording failed: {self.video_error}"
        )
        self._save()

    def stop(self) -> None:
        self.video_stop.set()
        if self.video_thread:
            self.video_thread.join(timeout=5)

    def _save(self) -> None:
        ffmpeg = which("ffmpeg")
        if self.frames:
            animation = [
                frame for frame in self.frames if frame.size == self.frames[0].size
            ]
            animation[0].save(
                self.root / "workflow.gif",
                save_all=True,
                append_images=animation[1:],
                duration=500,
                loop=0,
            )
            if ffmpeg:
                frames = self.root / "frames"
                frames.mkdir(exist_ok=True)
                for i, frame in enumerate(animation):
                    frame.save(frames / f"{i:04d}.png")
                subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-loglevel",
                        "error",
                        "-framerate",
                        "2",
                        "-i",
                        str(frames / "%04d.png"),
                        "-c:v",
                        "libx264",
                        "-vf",
                        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                        "-pix_fmt",
                        "yuv420p",
                        str(self.root / "workflow.mp4"),
                    ],
                    check=True,
                )
        if len(self.video_frames) > 1 and ffmpeg:
            video_dir = self.root / "motion-frames"
            video_dir.mkdir(exist_ok=True)
            sequence = []
            width, height = Image.open(BytesIO(self.video_frames[0][1])).size
            for index, (timestamp, data) in enumerate(self.video_frames):
                name = f"{index:05d}.jpg"
                frame = Image.open(BytesIO(data)).convert("RGB")
                if frame.size != (width, height):
                    frame = frame.resize((width, height))
                frame.save(video_dir / name)
                next_time = (
                    self.video_frames[index + 1][0]
                    if index + 1 < len(self.video_frames)
                    else timestamp + 0.1
                )
                sequence.extend(
                    (
                        f"file '{name}'",
                        f"duration {max(0.01, next_time - timestamp):.6f}",
                    )
                )
            (video_dir / "frames.txt").write_text("\n".join(sequence) + "\n")
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(video_dir / "frames.txt"),
                    "-vsync",
                    "vfr",
                    "-c:v",
                    "libx264",
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-pix_fmt",
                    "yuv420p",
                    str(self.root / "interaction.mp4"),
                ],
                check=True,
            )
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(self.root / "interaction.mp4"),
                    "-filter_complex",
                    "fps=10,scale=1280:-2,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a",
                    str(self.root / "interaction.gif"),
                ],
                check=True,
            )
        from waldo_commander.state import ui_state

        versions = {}
        for name in ("nicegui", "waldoctl", "parol6", "par6", "pinokin"):
            try:
                versions[name] = version(name)
            except PackageNotFoundError:
                continue
        digest = hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"]))
        for name in sorted(
            subprocess.check_output(
                ["git", "ls-files", "--others", "--exclude-standard", "-z"]
            )
            .decode()
            .split("\0")
        ):
            if name and Path(name).is_file():
                digest.update(name.encode())
                digest.update(Path(name).read_bytes())
        robot = ui_state.active_robot
        model = Path(ui_state.urdf_scene.urdf_path)
        dependencies = {
            name: str(Path(module.__file__).resolve())
            for name in ("nicegui", "waldoctl", robot.backend_package, "pinokin")
            if (module := sys.modules.get(name)) is not None and module.__file__
        }
        (self.root / "manifest.json").write_text(
            json.dumps(
                {
                    "commit": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], text=True
                    ).strip(),
                    "working_diff_sha256": digest.hexdigest(),
                    "versions": versions,
                    "dependency_sources": dependencies,
                    "backend": robot.backend_package,
                    "model": str(model),
                    "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                    "captures": self.captures,
                    "scenario": self.root.name,
                    "preserved_webgl_buffer": self.screen.selenium.execute_script("""
                        const root=document.querySelector('.wc-scene');
                        return root && getElement(root)?.core?.renderer.getContext().getContextAttributes()?.preserveDrawingBuffer;
                    """),
                    "reproduce": (
                        ".venv-scene-par6/bin/python"
                        if robot.backend_package == "par6"
                        else ".venv-scene/bin/python"
                    )
                    + " -m pytest "
                    + os.environ.get(
                        "PYTEST_CURRENT_TEST", "tests/test_joint_dial_screen.py"
                    ).split(" ")[0]
                    + " -q",
                    "program": str(ui_state.active_textarea.value),
                    "animation": "Actual captured UI states at 2 FPS; not a frame-time measurement",
                    "frames": len(self.frames),
                    "motion_frames": len(self.video_frames),
                    "motion_capture": "Screenshots after rendered frames"
                    if self.synchronized_frames
                    else "CDP screencast",
                    "motion_export": "enabled"
                    if ffmpeg
                    else "unavailable: ffmpeg not installed",
                },
                indent=2,
            )
        )
