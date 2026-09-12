# Camera localization

`locate_board` finds a printed ChArUco board using saved camera calibration.
It returns the board's pose in WRF, a host receipt timestamp, and detection
quality. It does not move the robot. Save a tool-mounted or fixed-camera
calibration in the [calibration panel](named-setup.md#camera-calibration)
before using it.

## Use Commander's camera in a program

Start the camera in Commander. A program launched from Commander can request
fresh images from that camera without opening the device again:

```python
from parol6 import RobotClient  # or: from par6 import RobotClient
from waldo_commander.camera_sources import CommanderCameraSource
from waldo_commander.setup import load_setup
from waldo_commander.skills import locate_board

setup = load_setup("bench")
camera = setup.cameras["overhead"]
source = CommanderCameraSource()

with RobotClient() as rbt:
    result = locate_board(rbt, camera, source, setup)
    if result.outcome == "found":
        print(result.pose, result.quality, result.received_at)
    else:
        print(result.outcome, result.reason)
```

With an async robot client, call
`await locate_board.async_call(rbt, camera, source, setup)`.
The **Skills** panel lets you select a saved calibration and setup, then
explicitly insert their fixed snapshots and `CommanderCameraSource()` into
Python. **Run once** opens and executes the call as a program. Its log includes
the detection outcome and, when found, the WRF pose and corner error.

| Result | Meaning |
|---|---|
| `found` | A pose passed the configured image-space checks. |
| `missing` | Too few usable board corners were detected; `pose` is `None`. |
| `rejected` | The image or pose solution was unusable, too small, inaccurate, or ambiguous; `pose` is `None`. Read `reason`. |

Camera disconnection, a stale image, or an expired acquisition deadline raises
`CameraUnavailable` or `TimeoutError`. A mismatched calibration raises
`ValueError`. These failures are distinct from observing a frame without a board.
The camera source, resolution, robot backend, and applicable tool/TCP or fixed
reference frame must match the calibration. Physical remounting, focus changes,
and replacing a device at the same index require recalibration.

## Image acquisition and timing

`CommanderCameraSource()` is inert until its `snapshot()` method is awaited.
Each launched program receives its own authenticated loopback connection to the
camera service. The connection closes when the process exits, fails to launch,
or is stopped. Session credentials are not included in exported calls. A camera
must already be active; requesting an image does not select or open a device.
Acquisition timeouts must be finite and greater than zero, up to five seconds.

Standalone programs supply an explicit object implementing `FrameSource`:

```python
from waldo_commander.camera import CameraSnapshot

class MyCameraSource:
    async def snapshot(self, *, timeout_s: float = 1.0) -> CameraSnapshot:
        # Use your capture API to obtain a new image within timeout_s.
        jpeg, receipt_time, sequence = await my_capture.next_jpeg(timeout_s)
        return CameraSnapshot(
            jpeg=jpeg,
            camera_id="my-camera",
            received_at=receipt_time,
            sequence=sequence,
            session_id=my_capture.session_id,
        )
```

Use immutable JPEG bytes and a Unix timestamp from the same host clock as the
program. Record receipt time when the new frame arrives, rather than when a
cached frame is requested. The calibration's camera identity must match this
source. Sequence numbers increase within a capture session; change the session
identity when acquisition restarts.

For a tool-mounted camera, the skill observes a referenced, stationary arm
before and after capture, checks the tool/TCP binding, and refuses more than
0.5 mm translation or 0.25 degrees rotation between observations. Keep the arm
still throughout acquisition. Host receipt time is **not exposure time**;
these checks do not synchronize a moving camera or detect motion that returns
to exactly the same pose between observations. A fixed camera does not need a
current TCP observation. The returned pose describes the captured scene; a
moving board may have changed position by the time subsequent code uses it.

## Preview uses explicit images

Preview never requests a live image. Supply an image with matching calibration
identity and resolution:

```python
from waldo_commander.camera_sources import ImageFixture

source = ImageFixture.from_file(
    "/path/to/board.jpg", camera_id=camera.camera_id,
)
result = locate_board(rbt, camera, source, setup)  # rbt is the preview client
```

For a tool camera, also supply `tcp_pose=` (an explicit WRF `Pose`) and `tool=`
(the matching `TcpCalibration`) to `ImageFixture.from_file`. Fixture timestamps
are zero to distinguish undated fixture images. Missing fixtures produce
`UnresolvedPreview`; missing boards preserve the `missing` branch. Live clients
refuse `ImageFixture` so test imagery cannot silently drive execution.

## Pure localization and acceptance limits

`waldo_commander.vision.localize_board(observation, camera, setup, backend=...)`
accepts an explicit `CameraSnapshot` and performs no acquisition or robot I/O.
Tool cameras additionally require `tcp_pose=` and `tool=`. Advanced callers can
provide a different measured `BoardSpec` through `board=`. By default, the board
geometry is the one recorded with the calibration.

The implementation detects ChArUco corners and uses OpenCV's
[planar IPPE solver](https://docs.opencv.org/4.13.0/d5/d1f/calib3d_solvePnP.html).
It compares planar pose alternatives and rejects materially different poses
with similar reprojection errors. Board axes follow OpenCV's board coordinates:
the origin is the first outer corner, with X along `squares_x`, Y along
`squares_y`, and translation in millimeters.

`LocalizationLimits` exposes maximum RMS and individual corner errors, minimum
image coverage, and ambiguity thresholds. Pass it as `limits=` to either API.
The defaults are 2 px RMS, 4 px maximum corner error, 0.2% image coverage, and
a 0.2 px alternative-solution gap when alternatives differ by more than
2 degrees or 2 mm. These are image acceptance rules, not measured positional
accuracy. Printing scale, calibration, lens distortion, lighting, board flatness,
and viewing angle affect the physical result. Inspect `DetectionQuality` and
validate the pose against your own fixture before using it in motion code.
