"""How scene state is written onto the wire: short op codes and rounded numbers.

Positions, rotations and joint values are rounded to 1e-7 (a tenth of a
micrometre or microradian), well below anything drawn but short to send;
rotations travel as quaternions.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

PRECISION = 7


def fixed(value: float) -> float:
    return round(float(value), PRECISION)


def fixed_all(values: Iterable[float]) -> list[float]:
    return [round(float(v), PRECISION) for v in values]


def significant(value: float) -> float:
    """``value`` to six significant digits: scales span 1e-3 to 1e3."""
    return float(f"{float(value):.6g}")


def quaternion(R: Sequence[Sequence[float]]) -> list[float]:
    """The unit quaternion ``[x, y, z, w]`` of rotation matrix ``R``."""
    (m00, m01, m02), (m10, m11, m12), (m20, m21, m22) = R
    trace = m00 + m11 + m22
    if trace > 0:
        s = 0.5 / math.sqrt(trace + 1.0)
        q = ((m21 - m12) * s, (m02 - m20) * s, (m10 - m01) * s, 0.25 / s)
    elif m00 > m11 and m00 > m22:
        s = 2.0 * math.sqrt(1.0 + m00 - m11 - m22)
        q = (0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s)
    elif m11 > m22:
        s = 2.0 * math.sqrt(1.0 + m11 - m00 - m22)
        q = ((m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s)
    else:
        s = 2.0 * math.sqrt(1.0 + m22 - m00 - m11)
        q = ((m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s)
    norm = math.sqrt(sum(c * c for c in q))
    return fixed_all(c / norm for c in q)


def euler_matrix(r_x: float, r_y: float, r_z: float) -> list[list[float]]:
    """``Rz @ Ry @ Rx``: turned about the fixed x, then y, then z axis."""
    cx, sx = math.cos(r_x), math.sin(r_x)
    cy, sy = math.cos(r_y), math.sin(r_y)
    cz, sz = math.cos(r_z), math.sin(r_z)
    return [
        [cy * cz, sx * sy * cz - cx * sz, cx * sy * cz + sx * sz],
        [cy * sz, sx * sy * sz + cx * cz, cx * sy * sz - sx * cz],
        [-sy, sx * cy, cx * cy],
    ]
