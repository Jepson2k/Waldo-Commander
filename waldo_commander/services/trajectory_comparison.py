"""Physics prediction differences at corresponding command times."""

from collections import defaultdict

import numpy as np
from scipy.spatial.transform import Rotation
from waldoctl import TickIndex


def finite_list(values, decimals: int) -> list:
    """*values* flattened and rounded, with NaN and infinities as None."""
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    out = np.round(flat, decimals).astype(object)
    out[~np.isfinite(flat)] = None
    return out.tolist()


def _aligned_rows(commanded: TickIndex, predicted: TickIndex, *, hold_endpoint: bool):
    by_command = defaultdict(list)
    for block in commanded.blocks:
        by_command[block.command].append(block)
    occurrences = defaultdict(int)
    ideal_rows = np.full(predicted.rows, -1, dtype=np.int64)
    settling = np.zeros(predicted.rows, dtype=bool)
    for block in predicted.blocks:
        occurrence = occurrences[block.command]
        occurrences[block.command] += 1
        peers = by_command.get(block.command, ())
        if occurrence >= len(peers) or not block.rows or block.error is not None:
            continue
        peer = peers[occurrence]
        if not peer.rows or peer.error is not None:
            continue
        rows = np.arange(
            block.start_row, min(predicted.rows, block.start_row + block.rows)
        )
        elapsed = np.arange(len(rows)) * predicted.row_dt_s
        offsets = np.floor(elapsed / commanded.row_dt_s + 0.5).astype(np.int64)
        beyond = offsets >= peer.rows
        if not hold_endpoint or commanded.stop != "completed":
            rows, offsets, beyond = rows[~beyond], offsets[~beyond], beyond[~beyond]
        ideal_rows[rows] = peer.start_row + np.minimum(offsets, peer.rows - 1)
        settling[rows] = beyond
    return ideal_rows, settling


#: The display thresholds a window's representative row is chosen by:
#: TCP mm, orientation °, any joint °.
_THRESHOLDS = (1.0, 0.5, 0.5)


def _errors(
    commanded: TickIndex,
    predicted: TickIndex,
    ideal_rows: np.ndarray,
    continuous: list[bool],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Each predicted row's TCP, orientation and joint error against its
    ideal row, and the ideal rows with unmatched ones as -1."""
    valid = (ideal_rows >= 0) & (ideal_rows < commanded.rows)
    safe_rows = np.clip(ideal_rows, 0, max(0, commanded.rows - 1))
    position = np.full(predicted.rows, np.nan)
    orientation = np.full(predicted.rows, np.nan)
    joint = np.full(predicted.rows, np.nan)
    if commanded.rows and predicted.rows:
        if commanded.valid is not None:
            valid &= commanded.valid[safe_rows]
        if predicted.valid is not None:
            valid &= predicted.valid
        valid &= np.isfinite(predicted.tcp).all(axis=1) & np.isfinite(
            commanded.tcp[safe_rows]
        ).all(axis=1)
        valid &= np.isfinite(predicted.joints_rad).all(axis=1) & np.isfinite(
            commanded.joints_rad[safe_rows]
        ).all(axis=1)
        selected = np.flatnonzero(valid)
        ideal = safe_rows[selected]
        position[selected] = (
            np.linalg.norm(
                predicted.tcp[selected, :3] - commanded.tcp[ideal, :3], axis=1
            )
            * 1000
        )
        if len(selected):
            a = Rotation.from_euler("XYZ", commanded.tcp[ideal, 3:6])
            b = Rotation.from_euler("XYZ", predicted.tcp[selected, 3:6])
            orientation[selected] = np.rad2deg((a.inv() * b).magnitude())
        delta = predicted.joints_rad[selected] - commanded.joints_rad[ideal]
        for index, wraps in enumerate(continuous):
            if wraps:
                delta[:, index] = np.arctan2(
                    np.sin(delta[:, index]), np.cos(delta[:, index])
                )
        joint[selected] = np.rad2deg(np.abs(delta).max(axis=1))
    ideal_rows = ideal_rows.copy()
    ideal_rows[~valid] = -1
    return ideal_rows, position, orientation, joint


def view_rows(kept: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """The sent row standing for each of *rows* (-1 stays -1): the kept row
    at or before it."""
    out = np.searchsorted(kept, rows, side="right") - 1
    return np.where(rows >= 0, out, -1)


def _thin(
    kept: np.ndarray,
    target: np.ndarray,
    pairs: np.ndarray,
    metrics: tuple[np.ndarray, ...],
    *extra: np.ndarray,
) -> tuple[np.ndarray, ...]:
    """The rows kept for the browser, each standing for the window up to the
    next: every metric's window maximum, so a short peak between kept rows
    still shows, and the pair (and *extra*) of the window's worst row."""
    if len(kept) == len(pairs):
        return (view_rows(target, pairs), *metrics, *extra)
    score = np.fmax.reduce(
        [m / limit for m, limit in zip(metrics, _THRESHOLDS, strict=True)]
    )
    score = np.where(np.isfinite(score), score, -np.inf)
    worst = np.maximum.reduceat(score, kept)
    lengths = np.diff(np.append(kept, len(score)))
    index = np.where(
        score == np.repeat(worst, lengths), np.arange(len(score)), len(score)
    )
    representative = np.minimum(np.minimum.reduceat(index, kept), len(score) - 1)
    maxima = []
    for m in metrics:
        peak = np.maximum.reduceat(np.where(np.isfinite(m), m, -np.inf), kept)
        maxima.append(np.where(np.isneginf(peak), np.nan, peak))
    return (
        view_rows(target, pairs[representative]),
        *maxima,
        *(e[representative] for e in extra),
    )


def comparison_samples(
    commanded: TickIndex,
    predicted: TickIndex,
    continuous: list[bool],
    kept: tuple[np.ndarray, np.ndarray] | None = None,
) -> dict:
    """Compare recorded poses at nearest sample times within command occurrences.

    Completed ideal endpoints can be held during prediction settling.
    Prediction coverage is never extended past its actual recorded samples.
    Both directions retain the selected sample's identity and pose. With
    *kept* (the commanded and predicted rows the browser is sent), each
    direction is given at those rows, paired rows numbered among them.
    """
    kept_ideal, kept_predicted = kept or (
        np.arange(commanded.rows),
        np.arange(predicted.rows),
    )
    rows, settling = _aligned_rows(commanded, predicted, hold_endpoint=True)
    pairs, *metrics = _errors(commanded, predicted, rows, continuous)
    pairs, *metrics, settling = _thin(
        kept_predicted, kept_ideal, pairs, tuple(metrics), settling
    )
    inverse, _ = _aligned_rows(predicted, commanded, hold_endpoint=False)
    back, *back_metrics = _errors(predicted, commanded, inverse, continuous)
    back, *back_metrics = _thin(kept_ideal, kept_predicted, back, tuple(back_metrics))

    def errors(paired: np.ndarray, values: list[np.ndarray], name: str) -> dict:
        position, orientation, joint = values
        return {
            name: paired.tolist(),
            "positionMm": finite_list(position, 4),
            "orientationDeg": finite_list(orientation, 4),
            "jointDeg": finite_list(joint, 4),
        }

    return dict(
        errors(pairs, metrics, "idealRows"),
        idealSamples=errors(back, back_metrics, "predictedRows"),
        settling=settling.tolist(),
        commandedDuration=commanded.duration_s,
        predictedDuration=predicted.duration_s,
        stop=predicted.stop,
    )
