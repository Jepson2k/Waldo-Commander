"""Eye-in-hand hand-eye calibration math.

ChArUco board generation/detection, camera-intrinsics calibration, and the
AX=XB hand-eye solve via ``cv2.calibrateHandEye``.  Consumed by the
hand-eye calibration panel; no UI imports here.

Units: millimeters and degrees on every public surface.  Transforms are
4x4 float64 homogeneous matrices with translation in mm — the same layout
as the robot status pose (``StatusBuffer.pose`` reshaped to (4, 4)).  The
board is constructed with square/marker lengths in mm, so every tvec
OpenCV produces downstream is already in mm.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Literal, Any, cast

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

MIN_CORNERS_PER_VIEW = 6
MIN_SAMPLES = 4
# Below this max pairwise relative rotation the camera-mount rotation is
# unobservable and the hand-eye solution is meaningless.
DEGENERATE_ROTATION_DEG = 3.0
WARN_ROTATION_DEG = 15.0
# Rotations about a single axis leave the mount translation along that axis
# unobservable, so a capture set also needs two sufficiently different axes.
AXIS_DIVERSITY_MIN_DEG = 9.0
# 0.5 rad, the axis-separation "golden rule" of Shi/Wang/Liu (IbPRIA 2005)
# that crigroup's handeye library applies per capture.  Accuracy keeps
# improving up to it: measured p90 translation error falls from ~120 mm at
# 5-10 deg of spread to ~18 mm here, so warn rather than reject below it.
AXIS_WARN_DEG = 28.6
# A near-duplicate capture pair has a rotation too small for its axis to be
# anything but noise; counting it would fake axis diversity.
AXIS_MIN_ROTATION_DEG = 3.0
# cv2.calibrateHandEye can return a non-orthonormal or left-handed rotation
# without raising; the solve is garbage whenever it does.
RIGID_TOL = 1e-4
_AXIS_BLOCK_ROWS = 256

HAND_EYE_METHODS: dict[str, int] = {
    "PARK": cv2.CALIB_HAND_EYE_PARK,
    "TSAI": cv2.CALIB_HAND_EYE_TSAI,
    "HORAUD": cv2.CALIB_HAND_EYE_HORAUD,
    "ANDREFF": cv2.CALIB_HAND_EYE_ANDREFF,
    "DANIILIDIS": cv2.CALIB_HAND_EYE_DANIILIDIS,
}

ARUCO_DICTIONARIES: dict[str, int] = {
    "DICT_4X4_50": cv2.aruco.DICT_4X4_50,
    "DICT_4X4_100": cv2.aruco.DICT_4X4_100,
    "DICT_5X5_100": cv2.aruco.DICT_5X5_100,
    "DICT_6X6_250": cv2.aruco.DICT_6X6_250,
}


class CalibrationError(RuntimeError):
    """A calibration step failed for a reason the user can act on."""


@dataclass(frozen=True)
class BoardSpec:
    """Printable ChArUco board geometry.

    ``square_mm``/``marker_mm`` are physical edge lengths of the printed
    board — the user must verify them against the printout with a ruler.
    """

    squares_x: int = 5
    squares_y: int = 7
    square_mm: float = 30.0
    marker_mm: float = 22.0
    dictionary: str = "DICT_4X4_50"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BoardSpec:
        return cls(
            squares_x=int(d["squares_x"]),
            squares_y=int(d["squares_y"]),
            square_mm=float(d["square_mm"]),
            marker_mm=float(d["marker_mm"]),
            dictionary=str(d["dictionary"]),
        )

    def validate(self) -> None:
        if self.squares_x < 3 or self.squares_y < 3:
            raise CalibrationError("Board needs at least 3x3 squares")
        if not (0 < self.marker_mm < self.square_mm):
            raise CalibrationError(
                "Marker size must be positive and smaller than square size"
            )
        if self.dictionary not in ARUCO_DICTIONARIES:
            raise CalibrationError(f"Unknown ArUco dictionary {self.dictionary!r}")
        # A ChArUco board carries a marker in every other square.
        markers = self.squares_x * self.squares_y // 2
        available = cv2.aruco.getPredefinedDictionary(
            ARUCO_DICTIONARIES[self.dictionary]
        ).bytesList.shape[0]
        if markers > available:
            raise CalibrationError(
                f"A {self.squares_x}x{self.squares_y} board needs {markers} "
                f"markers; {self.dictionary} has only {available}"
            )


def _hand_eye_solver() -> Callable[..., tuple[np.ndarray, np.ndarray]]:
    """``cv2.calibrateHandEye``, which the OpenCV stubs do not declare."""
    return cast(Any, cv2).calibrateHandEye


def _require_charuco_api() -> None:
    if not hasattr(cv2.aruco, "CharucoDetector") or not hasattr(
        cv2, "calibrateHandEye"
    ):
        raise CalibrationError(
            "This OpenCV build lacks the ChArUco/hand-eye API — hand-eye "
            "calibration requires opencv-python-headless >=4.8,<5"
        )


def make_board(spec: BoardSpec) -> cv2.aruco.CharucoBoard:
    _require_charuco_api()
    spec.validate()
    dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICTIONARIES[spec.dictionary])
    return cv2.aruco.CharucoBoard(
        (spec.squares_x, spec.squares_y), spec.square_mm, spec.marker_mm, dictionary
    )


def make_detector(spec: BoardSpec) -> cv2.aruco.CharucoDetector:
    return cv2.aruco.CharucoDetector(make_board(spec))


def board_png(spec: BoardSpec, dpi: int = 300, margin_squares: float = 0.5) -> bytes:
    """Render the board as a printable PNG at physical scale for ``dpi``."""
    board = make_board(spec)
    px_per_mm = dpi / 25.4
    margin_px = round(margin_squares * spec.square_mm * px_per_mm)
    width_px = round(spec.squares_x * spec.square_mm * px_per_mm) + 2 * margin_px
    height_px = round(spec.squares_y * spec.square_mm * px_per_mm) + 2 * margin_px
    image = board.generateImage((width_px, height_px), marginSize=margin_px)
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise CalibrationError("Failed to encode board PNG")
    return buf.tobytes()


@dataclass
class Detection:
    """ChArUco corners found in one camera frame."""

    corners: np.ndarray  # (N, 1, 2) float32
    ids: np.ndarray  # (N, 1) int32
    image_size: tuple[int, int]  # (width, height)
    n_markers: int


def decode_jpeg(data: bytes) -> np.ndarray | None:
    """Decode camera JPEG bytes to BGR; None when undecodable.

    The linuxpy backend passes raw v4l2 MJPEG through untouched and some
    cameras emit frames imdecode cannot parse — callers treat None as
    "no frame", never as an error.
    """
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def detect_board(
    image_bgr: np.ndarray, detector: cv2.aruco.CharucoDetector
) -> Detection | None:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    charuco_corners, charuco_ids, marker_corners, _marker_ids = detector.detectBoard(
        gray
    )
    if charuco_corners is None or charuco_ids is None:
        return None
    if len(charuco_corners) < MIN_CORNERS_PER_VIEW:
        return None
    h, w = gray.shape[:2]
    return Detection(
        corners=np.asarray(charuco_corners, dtype=np.float32),
        ids=np.asarray(charuco_ids, dtype=np.int32),
        image_size=(w, h),
        n_markers=0 if marker_corners is None else len(marker_corners),
    )


@dataclass
class HandEyeSample:
    """One synchronized (robot pose, board detection) pair."""

    T_base_gripper: np.ndarray  # (4, 4) float64, translation mm; TCP pose from status
    detection: Detection
    timestamp: float
    thumbnail: bytes | None = None  # small JPEG of the captured frame
    cells: frozenset[int] = frozenset()  # view_cells at capture
    tilt: str | None = None  # view_tilt at capture


# Where each view came from, for the coverage ring: the frame as a 3x3 grid
# of cells, and the direction the camera views the board from as one of eight
# 45-degree sectors, named from the camera's image (x right, y down).
TILT_MIN_DEG = 5.0
# Corners this close to one line (the spread across it over the spread along
# it) fix no tilt: IPPE returns NaN or an arbitrary rotation for them.
COLLINEAR_RATIO = 0.02
SECTORS: tuple[str, ...] = (
    "right",
    "down-right",
    "down",
    "down-left",
    "left",
    "up-left",
    "up",
    "up-right",
)
CELLS: tuple[str, ...] = (
    "top-left",
    "top",
    "top-right",
    "left",
    "centre",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
)
# Which gap to fill first: opposite directions before neighbours, and the
# frame's corners (where distortion is largest) before its edges and centre.
_SECTOR_PRIORITY = (
    "up",
    "down",
    "left",
    "right",
    "up-left",
    "down-right",
    "up-right",
    "down-left",
)
_CELL_PRIORITY = (
    "top-left",
    "bottom-right",
    "top-right",
    "bottom-left",
    "top",
    "bottom",
    "left",
    "right",
    "centre",
)


@dataclass(frozen=True)
class Coverage:
    """How the captured views spread over the frame and around the board."""

    cells: tuple[int, ...]  # views per cell, CELLS order
    sectors: tuple[int, ...]  # views per tilt sector, SECTORS order
    next_sector: str | None  # the empty sector to fill first; None when none is empty
    next_cell: str | None


def view_cells(detection: Detection, image_size: tuple[int, int]) -> frozenset[int]:
    """Cells of the 3x3 frame grid holding a detected corner."""
    w, h = image_size
    points = detection.corners.reshape(-1, 2)
    col = np.clip(np.floor(points[:, 0] * 3.0 / w), 0, 2).astype(int)
    row = np.clip(np.floor(points[:, 1] * 3.0 / h), 0, 2).astype(int)
    return frozenset((row * 3 + col).tolist())


def view_tilt(
    detection: Detection, board: cv2.aruco.CharucoBoard, image_size: tuple[int, int]
) -> str | None:
    """The side the camera views the board from, as a sector name, or None
    within ``TILT_MIN_DEG`` of straight on or when the corners fix no tilt.

    Solved against a nominal camera matrix (focal length = the frame's long
    side) because no intrinsics exist before the solve: a wrong focal length
    biases the tilt magnitude but leaves its direction intact.
    """
    obj, img = board.matchImagePoints(
        cast("Sequence[cv2.typing.MatLike]", detection.corners), detection.ids
    )
    if obj is None or img is None or len(obj) < 4:
        return None
    points = np.asarray(img, dtype=np.float64).reshape(-1, 2)
    if not np.all(np.isfinite(points)):
        return None
    spread = np.linalg.svd(points - points.mean(axis=0), compute_uv=False)
    if spread[1] <= COLLINEAR_RATIO * spread[0]:
        return None
    w, h = image_size
    f = float(max(w, h))
    K = np.array([[f, 0.0, w / 2.0], [0.0, f, h / 2.0], [0.0, 0.0, 1.0]])
    ok, rvec, _tvec = cv2.solvePnP(
        np.asarray(obj, dtype=np.float64),
        points,
        K,
        np.zeros(5),
        flags=cv2.SOLVEPNP_IPPE,
    )
    if not ok or not np.all(np.isfinite(rvec)) or not np.all(np.isfinite(_tvec)):
        return None
    zx, zy, zz = (float(v) for v in cv2.Rodrigues(rvec)[0][:, 2])
    if math.degrees(math.acos(min(abs(zz), 1.0))) < TILT_MIN_DEG:
        return None
    if zz < 0:
        zx, zy = -zx, -zy
    return SECTORS[round(math.degrees(math.atan2(zy, zx)) / 45.0) % 8]


def analyse_view(
    image_bgr: np.ndarray, detector: cv2.aruco.CharucoDetector
) -> tuple[Detection, frozenset[int], str | None] | None:
    """The board in a frame with the cells and tilt it was seen from, or None
    when no board is found."""
    detection = detect_board(image_bgr, detector)
    if detection is None:
        return None
    size = detection.image_size
    return (
        detection,
        view_cells(detection, size),
        view_tilt(detection, detector.getBoard(), size),
    )


def coverage(samples: Sequence[HandEyeSample]) -> Coverage:
    """Per-cell and per-sector view counts from what each sample stored at
    capture."""
    cells = [0] * len(CELLS)
    sectors = [0] * len(SECTORS)
    for s in samples:
        for c in s.cells:
            cells[c] += 1
        if s.tilt is not None:
            sectors[SECTORS.index(s.tilt)] += 1
    return Coverage(
        cells=tuple(cells),
        sectors=tuple(sectors),
        next_sector=next(
            (n for n in _SECTOR_PRIORITY if sectors[SECTORS.index(n)] == 0), None
        ),
        next_cell=next((n for n in _CELL_PRIORITY if cells[CELLS.index(n)] == 0), None),
    )


THUMBNAIL_WIDTH_PX = 160


def thumbnail_jpeg(frame: np.ndarray) -> bytes:
    """A view's picture at a size a panel can hold fifteen of."""
    height, width = frame.shape[:2]
    scale = THUMBNAIL_WIDTH_PX / max(width, 1)
    small = cv2.resize(
        frame,
        (THUMBNAIL_WIDTH_PX, max(int(height * scale), 1)),
        interpolation=cv2.INTER_AREA,
    )
    ok, encoded = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
    if not ok:
        raise CalibrationError("Could not encode the view thumbnail")
    return encoded.tobytes()


@dataclass
class IntrinsicsResult:
    camera_matrix: np.ndarray  # (3, 3)
    dist_coeffs: np.ndarray  # (5,) k1 k2 p1 p2 k3
    image_size: tuple[int, int]
    reproj_rms_px: float
    per_view_errors: list[float]
    rvecs: list[np.ndarray]  # target->cam rotation per view (Rodrigues)
    tvecs: list[np.ndarray]  # target->cam translation per view, mm


@dataclass
class HandEyeResult:
    T_camera_parent: np.ndarray  # camera → TCP (tool) or WRF (fixed), mm
    method: str
    rot_residual_deg: tuple[float, float]  # (mean, max) over motion pairs
    trans_residual_mm: tuple[float, float]
    target_spread_mm: float
    intrinsics: IntrinsicsResult
    n_views: int
    mount: Literal["tool", "fixed"] = "tool"


def _matched_points(
    samples: list[HandEyeSample], board: cv2.aruco.CharucoBoard
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    obj_points: list[np.ndarray] = []
    img_points: list[np.ndarray] = []
    for s in samples:
        obj, img = board.matchImagePoints(
            cast("Sequence[cv2.typing.MatLike]", s.detection.corners), s.detection.ids
        )
        if obj is None or img is None or len(obj) < MIN_CORNERS_PER_VIEW:
            raise CalibrationError(
                "A sample has too few usable corners — remove it and recapture"
            )
        obj_points.append(np.asarray(obj, dtype=np.float32))
        img_points.append(np.asarray(img, dtype=np.float32))
    return obj_points, img_points


def calibrate_intrinsics(
    samples: list[HandEyeSample], spec: BoardSpec
) -> IntrinsicsResult:
    if len(samples) < MIN_SAMPLES:
        raise CalibrationError(
            f"Need at least {MIN_SAMPLES} samples, have {len(samples)}"
        )
    sizes = {s.detection.image_size for s in samples}
    if len(sizes) != 1:
        raise CalibrationError("Samples were captured at different camera resolutions")
    image_size = samples[0].detection.image_size

    board = make_board(spec)
    obj_points, img_points = _matched_points(samples, board)
    try:
        # K3 is fixed at zero: on the few planar views a hand-eye session
        # produces, a free 6th-order term overfits and drags the focal
        # length with it.
        rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
            obj_points,
            img_points,
            image_size,
            np.zeros((3, 3)),
            np.zeros((5, 1)),
            flags=cv2.CALIB_FIX_K3,
        )
    except cv2.error as exc:
        raise CalibrationError(f"Intrinsics calibration failed: {exc}") from exc

    per_view: list[float] = []
    for obj, img, rvec, tvec in zip(obj_points, img_points, rvecs, tvecs, strict=True):
        projected, _ = cv2.projectPoints(obj, rvec, tvec, camera_matrix, dist_coeffs)
        err = np.linalg.norm(projected.reshape(-1, 2) - img.reshape(-1, 2), axis=1)
        per_view.append(float(np.sqrt(np.mean(err**2))))

    return IntrinsicsResult(
        camera_matrix=np.asarray(camera_matrix, dtype=np.float64),
        dist_coeffs=np.asarray(dist_coeffs, dtype=np.float64).ravel()[:5],
        image_size=image_size,
        reproj_rms_px=float(rms),
        per_view_errors=per_view,
        rvecs=[np.asarray(r, dtype=np.float64).ravel() for r in rvecs],
        tvecs=[np.asarray(t, dtype=np.float64).ravel() for t in tvecs],
    )


def motion_diversity(poses: list[np.ndarray]) -> tuple[float, float]:
    """(max pairwise relative-rotation deg, max angle between rotation axes deg).

    Both values gate degeneracy.  Only pairs rotating at least
    ``AXIS_MIN_ROTATION_DEG`` contribute an axis, so the second value is 0.0
    when fewer than two such rotations exist.
    """
    if len(poses) < 2:
        return 0.0, 0.0
    rotations = np.stack([p[:3, :3] for p in poses])
    i, j = np.triu_indices(len(poses), 1)
    rotvecs = Rotation.from_matrix(
        np.swapaxes(rotations[i], 1, 2) @ rotations[j]
    ).as_rotvec()
    angles = np.linalg.norm(rotvecs, axis=1)
    moving = np.degrees(angles) >= AXIS_MIN_ROTATION_DEG
    axes = rotvecs[moving] / angles[moving, None]
    # The widest pair of axes has the smallest |cos|. The pairs grow with the
    # square of the views, so their Gram matrix is taken a block of rows at a
    # time, with each axis's pairing with itself masked out.
    min_cos = 1.0
    for start in range(0, len(axes), _AXIS_BLOCK_ROWS):
        block = np.abs(axes[start : start + _AXIS_BLOCK_ROWS] @ axes.T)
        rows = np.arange(len(block))
        block[rows, start + rows] = np.inf
        min_cos = min(min_cos, float(block.min()))
    max_rotation = math.degrees(float(angles.max()))
    return max_rotation, math.degrees(math.acos(max(min_cos, 0.0)))


def _target2cam_matrix(rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = cv2.Rodrigues(rvec)[0]
    T[:3, 3] = tvec
    return T


def _hand_eye_inputs(
    samples: list[HandEyeSample],
    intrinsics: IntrinsicsResult,
) -> tuple[list[np.ndarray], list[np.ndarray], list[np.ndarray], list[np.ndarray]]:
    """Build calibrateHandEye inputs for an eye-in-hand camera; the single
    place an eye-to-hand mode would later invert the robot poses."""
    R_gripper2base = [s.T_base_gripper[:3, :3].copy() for s in samples]
    t_gripper2base = [s.T_base_gripper[:3, 3].copy() for s in samples]
    R_target2cam = [cv2.Rodrigues(r)[0] for r in intrinsics.rvecs]
    t_target2cam = [t.copy() for t in intrinsics.tvecs]
    return R_gripper2base, t_gripper2base, R_target2cam, t_target2cam


def solve_hand_eye(
    samples: list[HandEyeSample],
    spec: BoardSpec,
    *,
    method: str = "PARK",
    intrinsics: IntrinsicsResult | None = None,
    mount: Literal["tool", "fixed"] = "tool",
) -> HandEyeResult:
    if mount not in {"tool", "fixed"}:
        raise CalibrationError("Camera mount must be tool or fixed")
    if mount == "fixed":
        # inv(T_base_tcp) @ T_base_camera @ T_camera_board is the fixed
        # board-to-TCP transform, so the same AX=XB solve applies.
        samples = [
            HandEyeSample(np.linalg.inv(s.T_base_gripper), s.detection, s.timestamp)
            for s in samples
        ]
    if method not in HAND_EYE_METHODS:
        raise CalibrationError(f"Unknown hand-eye method {method!r}")
    if len(samples) < MIN_SAMPLES:
        raise CalibrationError(
            f"Need at least {MIN_SAMPLES} samples, have {len(samples)}"
        )

    gripper_poses = [s.T_base_gripper for s in samples]
    max_rot, max_axis = motion_diversity(gripper_poses)
    if max_rot < DEGENERATE_ROTATION_DEG:
        raise CalibrationError(
            "Pure-translation motion: rotation of the camera mount is "
            "unobservable — rotate the wrist between captures"
        )
    if max_axis < AXIS_DIVERSITY_MIN_DEG:
        raise CalibrationError(
            "Single-axis motion: the camera offset along that axis is "
            "unobservable — rotate about a second wrist axis between captures"
        )

    if intrinsics is None:
        intrinsics = calibrate_intrinsics(samples, spec)

    R_g2b, t_g2b, R_t2c, t_t2c = _hand_eye_inputs(samples, intrinsics)
    try:
        R_cam2gripper, t_cam2gripper = _hand_eye_solver()(
            R_g2b, t_g2b, R_t2c, t_t2c, method=HAND_EYE_METHODS[method]
        )
    except cv2.error as exc:
        raise CalibrationError(f"Hand-eye solve failed: {exc}") from exc

    X = np.eye(4)
    X[:3, :3] = R_cam2gripper
    X[:3, 3] = np.asarray(t_cam2gripper, dtype=np.float64).ravel()
    R_x = X[:3, :3]
    if (
        not np.all(np.isfinite(X))
        or not np.allclose(R_x.T @ R_x, np.eye(3), atol=RIGID_TOL)
        or abs(float(np.linalg.det(R_x)) - 1.0) > RIGID_TOL
    ):
        raise CalibrationError(
            f"{method} returned a non-rigid transform — the capture set is "
            "too ill-conditioned; avoid near-180° flips between views"
        )

    T_target2cam = [
        _target2cam_matrix(r, t)
        for r, t in zip(intrinsics.rvecs, intrinsics.tvecs, strict=True)
    ]

    # AX = XB over consecutive motion pairs: A from robot FK, B from the
    # board observations; D is the closure error that would be identity for
    # a perfect calibration.
    rot_errs: list[float] = []
    trans_errs: list[float] = []
    for i in range(len(samples) - 1):
        A = np.linalg.inv(gripper_poses[i + 1]) @ gripper_poses[i]
        B = T_target2cam[i + 1] @ np.linalg.inv(T_target2cam[i])
        D = np.linalg.inv(A @ X) @ (X @ B)
        rot_errs.append(
            math.degrees(
                float(np.linalg.norm(Rotation.from_matrix(D[:3, :3]).as_rotvec()))
            )
        )
        trans_errs.append(float(np.linalg.norm(D[:3, 3])))

    # The board sits still while the robot moves, so its reconstructed
    # base-frame position should be identical across views; the scatter is
    # the most intuitive quality number.
    target_positions = np.array(
        [(g @ X @ T)[:3, 3] for g, T in zip(gripper_poses, T_target2cam, strict=True)]
    )
    target_spread = float(np.mean(np.std(target_positions, axis=0)))

    return HandEyeResult(
        T_camera_parent=X,
        mount=mount,
        method=method,
        rot_residual_deg=(float(np.mean(rot_errs)), float(np.max(rot_errs))),
        trans_residual_mm=(float(np.mean(trans_errs)), float(np.max(trans_errs))),
        target_spread_mm=target_spread,
        intrinsics=intrinsics,
        n_views=len(samples),
    )


def matrix_to_xyz_rpy(
    T: np.ndarray,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """(x, y, z) mm and intrinsic-XYZ Euler deg — the pinokin display convention."""
    x, y, z = (float(v) for v in T[:3, 3])
    rx, ry, rz = Rotation.from_matrix(T[:3, :3]).as_euler("XYZ", degrees=True)
    return (x, y, z), (float(rx), float(ry), float(rz))


def to_storage_dict(
    result: HandEyeResult,
    spec: BoardSpec,
    tool_key: str,
    tcp_offset: dict[str, float],
    timestamp: str,
) -> dict[str, Any]:
    if result.mount != "tool":
        raise CalibrationError(
            "Per-tool hand-eye storage cannot represent a fixed camera"
        )
    return {
        "version": 1,
        "tool_key": tool_key,
        "timestamp": timestamp,
        "board": spec.to_dict(),
        "image_size": list(result.intrinsics.image_size),
        "camera_matrix": result.intrinsics.camera_matrix.ravel().tolist(),
        "dist_coeffs": result.intrinsics.dist_coeffs.ravel().tolist(),
        "reproj_rms_px": result.intrinsics.reproj_rms_px,
        "T_cam2gripper_mm": result.T_camera_parent.ravel().tolist(),
        "method": result.method,
        "rot_residual_deg": {
            "mean": result.rot_residual_deg[0],
            "max": result.rot_residual_deg[1],
        },
        "trans_residual_mm": {
            "mean": result.trans_residual_mm[0],
            "max": result.trans_residual_mm[1],
        },
        "target_spread_mm": result.target_spread_mm,
        "n_samples": result.n_views,
        "tcp_offset_snapshot": dict(tcp_offset),
    }


def from_storage_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a stored calibration for display."""
    T = np.asarray(d["T_cam2gripper_mm"], dtype=np.float64).reshape(4, 4)
    xyz, rpy = matrix_to_xyz_rpy(T)
    return {
        "tool_key": str(d.get("tool_key", "")),
        "timestamp": str(d.get("timestamp", "")),
        "T_cam2gripper": T,
        "xyz_mm": xyz,
        "rpy_deg": rpy,
        "reproj_rms_px": float(d.get("reproj_rms_px", float("nan"))),
        "n_samples": int(d.get("n_samples", 0)),
        "method": str(d.get("method", "")),
        "tcp_offset_snapshot": dict(d.get("tcp_offset_snapshot", {})),
        "board": dict(d.get("board", {})),
    }
