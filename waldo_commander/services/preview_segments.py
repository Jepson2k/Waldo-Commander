"""Path segments from the commanded record.

The backend's record is the truth about what a program tells the arm to
do: one block per command, rows at the record's rate. What the scene, the
scrub bar and the editor draw from it is a list of segments — one per
command that moved, split where the record marks rows unreachable — and
this is the only place that list is derived, so every consumer indexes the
same shapes by the same ``command``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

import numpy as np
from waldoctl import (
    CommandNote,
    PathSegment,
    ProgramTarget,
    ShapeChange,
    TickIndex,
    ToolAction,
    ToolSelection,
)

from waldo_commander.common.theme import SceneColors, get_color_for_move_type

#: A marker segment: a checkpoint or a refusal that owns no rows is drawn
#: as nothing in the scene and as a zero-width division on the scrub bar.
MARKER_COLOR = "#00000000"

#: Within this many metres two TCP samples are one place: a hold.
_STILL_M = 1e-6


def _moves(points: Sequence[Sequence[float]]) -> bool:
    """Whether a run of TCP points goes anywhere."""
    if len(points) < 2:
        return False
    first = np.asarray(points[0], dtype=np.float64)
    return bool(np.abs(np.asarray(points, dtype=np.float64) - first).max() > _STILL_M)


def _runs(valid: np.ndarray) -> list[tuple[int, int, bool]]:
    """Consecutive runs of one validity: ``(start, end, valid)``."""
    out: list[tuple[int, int, bool]] = []
    i, n = 0, len(valid)
    while i < n:
        ok = bool(valid[i])
        j = i + 1
        while j < n and bool(valid[j]) == ok:
            j += 1
        out.append((i, j, ok))
        i = j
    return out


def note_for(notes: Sequence[CommandNote], command: int) -> CommandNote:
    """The host's note on *command*, or an empty one for a command the
    backend queued on the program's behalf."""
    if 0 <= command < len(notes):
        return notes[command]
    return CommandNote(line_number=0)


def segments_from_record(
    record: TickIndex,
    notes: Sequence[CommandNote],
    collisions: Mapping[int, int] | None = None,
) -> list[PathSegment]:
    """One segment per block that owns rows, in program order.

    A block whose rows the record marks partly unreachable is split into
    runs of one validity, each run a segment sharing the block's
    ``command``. A block that owns no rows is kept only when it carries a
    checkpoint label or a refusal, as a zero-width marker. *collisions*
    maps a command to the first row of its block that collides.
    """
    collisions = collisions or {}
    dt = record.row_dt_s
    out: list[PathSegment] = []
    for block in record.blocks:
        note = note_for(notes, block.command)
        line = note.line_number or block.line_number or 0
        move_type = block.move_type or note.method or "unknown"
        if block.rows == 0:
            if note.checkpoint is None and block.error is None:
                continue
            out.append(
                PathSegment(
                    points=[],
                    color=MARKER_COLOR,
                    is_valid=block.error is None,
                    line_number=line,
                    command=block.command,
                    start_row=block.start_row,
                    rows=0,
                    move_type="checkpoint" if note.checkpoint else move_type,
                    is_dashed=False,
                    show_arrows=False,
                    estimated_duration=0.0,
                    requested_duration=note.requested_duration,
                    timing_feasible=True,
                    checkpoint=note.checkpoint,
                    is_travel=note.travel,
                )
            )
            continue

        first, last = block.start_row, block.start_row + block.rows
        duration = block.rows * dt
        requested = note.requested_duration
        feasible = requested is None or duration <= requested * 1.05
        collision = collisions.get(block.command)
        valid = record.valid[first:last] if record.valid is not None else None
        if valid is not None and not bool(np.all(valid)):
            runs = _runs(valid)
        else:
            runs = [(0, block.rows, block.error is None)]

        for start, end, ok in runs:
            # One overlapping point at a run boundary keeps the drawn path
            # continuous where its colour changes.
            lo = max(start - 1, 0)
            hi = min(end + 1, block.rows)
            points = record.tcp[first + lo : first + hi, :3].astype(float).tolist()
            moving = _moves(points)
            if not moving:
                # A hold owns its time on the bar and no length in the scene.
                points = [points[0], points[-1]]
            closes = end == block.rows
            hit = None
            if collision is not None and start <= collision < end:
                hit = collision - start
            out.append(
                PathSegment(
                    points=points,
                    color=(
                        SceneColors.COLLISION_HEX
                        if hit is not None and ok
                        else get_color_for_move_type(move_type, ok)
                    ),
                    is_valid=ok,
                    line_number=line,
                    command=block.command,
                    start_row=first + start,
                    rows=end - start,
                    move_type=move_type,
                    is_dashed=len(points) <= 2,
                    show_arrows=ok and moving,
                    estimated_duration=duration if closes else None,
                    requested_duration=requested if closes else None,
                    timing_feasible=feasible,
                    checkpoint=note.checkpoint if closes else None,
                    is_travel=note.travel,
                    collision_step=hit,
                )
            )
    return out


def command_segments(segments: Sequence[PathSegment]) -> dict[int, int]:
    """The first segment index rendering each command."""
    out: dict[int, int] = {}
    for index, segment in enumerate(segments):
        if segment.command is not None:
            out.setdefault(segment.command, index)
    return out


def preceding_segment(segments: Sequence[PathSegment], command: int) -> int:
    """The last segment of a command before *command*, or -1: the one a
    boundary recorded after it applies from."""
    found = -1
    for index, segment in enumerate(segments):
        if segment.command is not None and segment.command < command:
            found = index
    return found


def index_boundaries(
    segments: Sequence[PathSegment],
    items: Iterable[ToolSelection | ShapeChange],
) -> None:
    """Set each boundary's ``segment_index`` from its ``command``: the
    boundary applies to every segment after the last one it followed."""
    for item in items:
        if item.command >= 0:
            item.segment_index = preceding_segment(segments, item.command)


def tool_actions_from_record(
    actions: Iterable[ToolAction],
    record: TickIndex,
    segments: Sequence[PathSegment],
) -> list[ToolAction]:
    """Each tool action with what its block says: how long the arm held
    for it, where the TCP stood, and the segment it follows."""
    dt = record.row_dt_s
    out: list[ToolAction] = []
    for action in actions:
        block = (
            record.blocks[action.command]
            if 0 <= action.command < len(record.blocks)
            else None
        )
        tcp_pose = action.tcp_pose
        duration = action.estimated_duration
        if block is not None:
            duration = block.rows * dt
            standing = (
                block.start_row + block.rows - 1 if block.rows else block.start_row - 1
            )
            if 0 <= standing < record.rows:
                tcp_pose = record.tcp[standing].astype(float).tolist()
        out.append(
            replace(
                action,
                tcp_pose=tcp_pose,
                estimated_duration=duration,
                segment_index=preceding_segment(segments, action.command)
                if action.command >= 0
                else action.segment_index,
            )
        )
    return out


def targets_from_record(
    record: TickIndex, notes: Sequence[CommandNote]
) -> list[ProgramTarget]:
    """A drag-to-edit target at the end of every move written with literal
    coordinates that the record shows reaching somewhere."""
    out: list[ProgramTarget] = []
    for block in record.blocks:
        note = note_for(notes, block.command)
        if not note.literal or block.rows == 0 or block.error is not None:
            continue
        end = block.start_row + block.rows - 1
        out.append(
            ProgramTarget(
                id=f"auto_{note.line_number}",
                line_number=note.line_number,
                pose=record.tcp[end].astype(float).tolist(),
                move_type=block.move_type or note.method,
                scene_object_id="",
            )
        )
    return out
