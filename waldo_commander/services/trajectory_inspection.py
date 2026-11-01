"""Read-only recorded motion for one scene view."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import waldoctl
from nicegui import background_tasks

from waldo_commander.common.theme import hex_of
from waldo_commander.services.preview_segments import marker_block, moves, note_for
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.services.timeline import distinct_prediction, selection_before
from waldo_commander.services.trajectory_comparison import (
    comparison_samples,
    finite_list,
    view_rows,
)

logger = logging.getLogger(__name__)

#: Decimals the browser gets: µm and µrad, well under what it can draw.
_DECIMALS = 6

#: The most rows of one record the browser is sent, besides each block's
#: first and last: a long program is thinned to keep every send short.
MAX_SENT_ROWS = 3000


def _cuts(record) -> np.ndarray:
    """Where *record* is cut into spans for the browser: every block's first
    row and the row after its last."""
    rows = record.rows
    cuts = {0, rows}
    for b in record.blocks:
        start = min(b.start_row, rows)
        cuts.add(start)
        cuts.add(min(start + b.rows, rows))
    return np.array(sorted(cuts))


def kept_rows(record) -> np.ndarray:
    """The rows of *record* the browser is sent: each span between block
    bounds thinned on a grid of its own, with its last row, so a block that
    did not change keeps the same rows wherever it now starts."""
    rows = record.rows
    stride = -(-rows // MAX_SENT_ROWS)
    if stride <= 1:
        return np.arange(rows)
    cuts = _cuts(record)
    marks = [
        np.array([min(b.start_row, rows - 1)]) for b in record.blocks if not b.rows
    ]
    for a, b in zip(cuts[:-1], cuts[1:], strict=True):
        if b > a:
            marks += [np.arange(a, b, stride), np.array([b - 1])]
    return np.unique(np.concatenate(marks))


def _chunk_payload(arrays: tuple) -> dict:
    """One span's rows as the browser reads them."""
    offsets, q, tcp, closed, valid, objects = arrays
    return {
        "offsets": offsets.tolist(),
        "q": finite_list(q, _DECIMALS),
        "tcp": finite_list(tcp, _DECIMALS),
        "closed": finite_list(closed, _DECIMALS),
        "valid": valid.tolist() if valid is not None else None,
        "objects": [finite_list(p, _DECIMALS) for p in objects],
    }


@dataclass(frozen=True, slots=True)
class PathPick:
    """A pinned point of a drawn path: the record (0 commanded, 1
    predicted), its row, the drawn block, and that block's command run."""

    record: int
    row: int
    block: int
    command: int
    occurrence: int
    valid: bool


class TrajectoryInspection:
    def __init__(self, owner) -> None:
        self.owner = owner
        self.key = None
        self.generation = 0
        self.sequence = -1
        self.data: dict[str, Any] | None = None
        self.pinned = False
        self.cursor: PathPick | None = None
        #: Per record, each drawn block's index by (command, occurrence).
        self._block_index: list[dict[tuple[int, int], int]] = []
        #: Per record, the record row of each row the browser was sent.
        self._kept: list[np.ndarray] = []
        self._comparison_task: asyncio.Task | None = None
        self._status_key = None
        #: The commanded record's entry, kept while only the prediction changes.
        self._commanded: tuple[tuple, dict, np.ndarray, dict] | None = None
        #: Each span the paths are made of, by its content's key, as sent.
        self._payloads: dict[str, dict] = {}
        #: The spans the browser holds.
        self._sent: set[str] = set()

    def remount(self) -> None:
        """A view mounted again holds no spans: it is sent the current paths
        whole, starts with no pin and numbers its events afresh."""
        self.pinned = False
        self.cursor = None
        self.sequence = -1
        if self.data is not None and self.owner.scene:
            self._sent = set(self._payloads)
            self.owner.scene.set_interaction(
                pathData=dict(self.data, chunks=self._payloads)
            )
        self.update()

    def clear(self) -> None:
        if self._comparison_task is not None:
            self._comparison_task.cancel()
            self._comparison_task = None
        pinned = self.pinned
        program = self.data["program"] if self.data else None
        self.key = None
        self.data = None
        self.pinned = False
        self.cursor = None
        self._block_index = []
        self._kept = []
        if self.owner.scene:
            self.owner.scene.set_interaction(pathData=None, comparisonData=None)
            if pinned:
                from waldo_commander.components.playback import playback

                if playback._ui_client is self.owner.scene.client:
                    playback.inspect_path(self, 0, program=program)

    def update(self) -> None:
        from waldo_commander.services.path_visualizer import path_visualizer

        scene = self.owner.scene
        simulator = waldoctl.commander.status.simulator_active
        active = waldoctl.commander.programs.active
        view = waldoctl.commander.settings.view
        if scene:
            scene.set_interaction(
                simulator=simulator,
                pathScrubAvailable=simulator
                and not is_any_program_running()
                and not self.owner.editing_target,
                comparisonVisible=view.predicted_visible,
            )
        dry = active.dry_run if active else None
        record = dry.commanded if dry else None
        status = path_visualizer.prediction_status(active)
        if not view.paths_visible:
            status = {"state": "unavailable", "reason": ""}

        def update_status():
            key = (
                active.id if active else None,
                self.generation,
                tuple(status.items()),
            )
            if scene and key != self._status_key:
                self._status_key = key
                scene.set_interaction(comparisonStatus=status)

        if (
            not scene
            or active is None
            or dry is None
            or not view.paths_visible
            or record is None
            or not record.rows
            or dry.commanded_revision != dry.revision
        ):
            if self.data is not None:
                self.clear()
            if scene:
                update_status()
            return
        predicted = distinct_prediction(record, dry.predicted_current)
        key = (active.id, dry.revision, id(record), id(predicted))
        if key == self.key:
            update_status()
            return
        self.clear()
        self.key = key
        self.generation += 1
        self.sequence = -1
        commanded_key = key[:3]
        if self._commanded is None or self._commanded[0] != commanded_key:
            kept = kept_rows(record)
            self._commanded = (
                commanded_key,
                *self._record(record, dry, "Commanded", kept),
            )
        records = [self._commanded[1]]
        self._kept = [self._commanded[2]]
        spans = dict(self._commanded[3])
        if predicted is not None and predicted.rows:
            kept = kept_rows(predicted)
            entry, kept, predicted_spans = self._record(
                predicted, dry, "Predicted", kept
            )
            records.append(entry)
            self._kept.append(kept)
            spans.update(predicted_spans)
        self._payloads = {
            k: self._payloads.get(k) or _chunk_payload(arrays)
            for k, arrays in spans.items()
        }
        unsent = {k: v for k, v in self._payloads.items() if k not in self._sent}
        self._sent = set(self._payloads)
        self._block_index = [
            {
                (b["command"], b["commandOccurrence"]): i
                for i, b in enumerate(r["blocks"])
            }
            for r in records
        ]
        pairs = {
            (b["tool"]["key"], b["tool"]["variant"])
            for r in records
            for b in r["blocks"]
        }
        tools = {}
        for tool_key, variant_key in pairs:
            meshes, motions = self.owner.tool_parts(tool_key, variant_key)
            tools[f"{tool_key}/{variant_key}"] = {
                "meshes": [
                    {
                        "url": self.owner._stl_to_url(m.file),
                        "origin": m.origin,
                        "rpy": m.rpy,
                        "role": m.role.value,
                    }
                    for m in meshes
                    if m.file
                ],
                "motions": [
                    dict(asdict(m), role=m.role.value, kind=type(m).__name__)
                    for m in motions
                ],
            }
        self.data = {
            "revision": self.generation,
            "program": active.id,
            "records": records,
            "meshes": [m.id for m in self.owner._robot_meshes],
            "joints": [self.owner.joint_groups[n].id for n in self.owner.joint_names],
            "panel": [
                self.owner._panel_mapping(i) for i in range(len(self.owner.joint_names))
            ],
            "anchor": self.owner.tcp_anchor.id if self.owner.tcp_anchor else None,
            "tools": tools,
            "scale": self.owner._stl_scale,
            "color": hex_of("action"),
            "collisionColor": hex_of("scene-collision"),
            "predictedColor": hex_of("physics-contact"),
            "predictionVisible": view.predicted_visible,
        }
        scene.set_interaction(pathData=dict(self.data, chunks=unsent))
        self._status_key = None
        update_status()
        if predicted is not None and predicted.rows:
            generation = self.generation
            continuous = self.data["records"][0]["continuous"]
            kept = (self._kept[0], self._kept[1])

            async def compare():
                try:
                    result = await asyncio.to_thread(
                        comparison_samples, record, predicted, continuous, kept
                    )
                except Exception:
                    logger.exception("Trajectory comparison failed")
                    if self.generation == generation and self.data is not None:
                        scene.set_interaction(
                            comparisonStatus={
                                "state": "failed",
                                "reason": "Trajectory comparison failed",
                            }
                        )
                    return
                if self.generation == generation and self.data is not None:
                    scene.set_interaction(
                        comparisonData=dict(result, revision=generation)
                    )

            self._comparison_task = background_tasks.create(compare())

    def _record(
        self, record, dry, label: str, kept: np.ndarray
    ) -> tuple[dict, np.ndarray, dict[str, tuple]]:
        """*record*'s entry for the browser, the rows it keeps, and its spans'
        rows by each span's content key."""
        from waldo_commander.services.urdf_scene.urdf_scene import object_render_poses

        blocks = []
        occurrences = defaultdict(int)
        actions = {a.command: a.method for a in dry.tool_actions}
        live = waldoctl.commander.status.tool
        initial = {"key": live.key or "NONE", "variant": live.variant_key or ""}
        collisions: dict[int, list[int]] = defaultdict(list)
        if record is dry.commanded:
            for segment in dry.path_segments:
                if segment.command is not None and segment.collision_step is not None:
                    collisions[segment.command].append(
                        segment.start_row + segment.collision_step
                    )
        for b in record.blocks:
            command_occurrence = occurrences[b.command]
            occurrences[b.command] += 1
            note = note_for(dry.commands, b.command)
            if not b.rows and not marker_block(b, note):
                continue
            start = min(b.start_row, record.rows - 1)
            stationary = not moves(record.tcp[start : start + b.rows, :3])
            selected = selection_before(dry.tool_selections, b.command)
            first, last = view_rows(
                kept, np.array([start, min(start + max(1, b.rows), record.rows) - 1])
            )
            blocks.append(
                {
                    "commandOccurrence": command_occurrence,
                    "command": b.command,
                    "start": int(first),
                    "end": int(last) + 1,
                    "instant": not b.rows,
                    "at": b.start_row * record.row_dt_s,
                    "line": note.line_number or b.line_number or 0,
                    "name": f"tool.{actions[b.command]}"
                    if b.command in actions
                    else note.method or b.move_type or "Action",
                    "stationary": stationary,
                    "valid": b.error is None,
                    "collisionRow": next(
                        (
                            row
                            for row in sorted(collisions[b.command])
                            if start <= row < start + b.rows
                        ),
                        None,
                    ),
                    "tool": initial
                    if selected is None
                    else {
                        "key": selected.tool_key,
                        "variant": selected.variant_key or "",
                    },
                }
            )
        nodes: list[int] = []
        poses: list[np.ndarray] = []
        for obj in record.objects:
            key, drawn = self.owner._object_entry(obj.name)
            node = self.owner._shape_objects.get(key) if drawn else None
            if node is not None and len(obj.poses):
                nodes.append(node.id)
                at = obj.poses[np.minimum(kept, len(obj.poses) - 1)]
                poses.append(
                    np.round(object_render_poses(drawn.geometry[0], at), _DECIMALS)
                )
        q = np.round(record.joints_rad[kept], _DECIMALS)
        tcp = np.round(record.tcp[kept], _DECIMALS)
        closed = np.round(record.tool_closed[kept], _DECIMALS)
        valid = record.valid[kept] if record.valid is not None else None
        spans: list[dict] = []
        arrays: dict[str, tuple] = {}
        cuts = _cuts(record)
        for a, b in zip(cuts[:-1], cuts[1:], strict=True):
            i, j = np.searchsorted(kept, [a, b])
            if j <= i:
                continue
            part = (
                (kept[i:j] - a).astype(np.int64),
                q[i:j],
                tcp[i:j],
                closed[i:j],
                valid[i:j] if valid is not None else None,
                tuple(p[i:j] for p in poses),
            )
            digest = hashlib.blake2b(repr(nodes).encode(), digest_size=12)
            for values in (*part[:5], *part[5]):
                if values is not None:
                    digest.update(str(values.shape).encode())
                    digest.update(np.ascontiguousarray(values).tobytes())
            span_key = digest.hexdigest()
            arrays.setdefault(span_key, part)
            spans.append({"key": span_key, "base": int(a)})
        entry = {
            "label": label,
            "dt": record.row_dt_s,
            "rows": len(kept),
            "dof": record.joints_rad.shape[1],
            "continuous": [
                self.owner.urdf_model.joint_map[n].joint_type == "continuous"
                for n in self.owner.joint_names
            ],
            "blocks": blocks,
            "objects": nodes,
            "spans": spans,
        }
        return entry, kept, arrays

    def sweep_block(self, path: Any) -> dict | None:
        if not isinstance(path, dict) or self.data is None:
            return None
        active = waldoctl.commander.programs.active
        if (
            active is None
            or self.key is None
            or self.key[:2] != (active.id, active.dry_run.revision)
            or path.get("revision") != self.generation
        ):
            return None
        index = path.get("block")
        blocks = self.data["records"][0]["blocks"]
        if not isinstance(index, int) or not 0 <= index < len(blocks):
            return None
        block = blocks[index]
        return (
            block
            if not block["instant"] and block["end"] - block["start"] >= 2
            else None
        )

    def view_row(self, record: int, row: int) -> int:
        """The sent row of *record* standing for its row *row*."""
        if record >= len(self._kept):
            return row
        return int(view_rows(self._kept[record], np.array([row]))[0])

    def block_index(self, record: int, command: int, occurrence: int) -> int | None:
        """Where *command*'s *occurrence*-th run is among *record*'s drawn
        blocks, if it is drawn."""
        if record >= len(self._block_index):
            return None
        return self._block_index[record].get((command, occurrence))

    def handle(self, args: Any) -> None:
        if not isinstance(args, dict) or self.data is None:
            return
        active = waldoctl.commander.programs.active
        if (
            active is None
            or self.key is None
            or self.key[:2] != (active.id, active.dry_run.revision)
        ):
            self.clear()
            return
        seq = args.get("seq")
        if (
            args.get("revision") != self.generation
            or not isinstance(seq, int)
            or seq <= self.sequence
        ):
            return
        self.sequence = seq
        record_index, row = args.get("record"), args.get("row")
        if not isinstance(record_index, int) or not isinstance(row, int):
            return
        if not 0 <= record_index < len(self.data["records"]):
            return
        record = self.data["records"][record_index]
        if not 0 <= row < record["rows"]:
            return
        block_index = args.get("block")
        if not isinstance(block_index, int) or not 0 <= block_index < len(
            record["blocks"]
        ):
            return
        block = record["blocks"][block_index]
        if not block["start"] <= row < block["end"]:
            return
        self.pinned = bool(args.get("pinned"))
        self.cursor = (
            PathPick(
                record_index,
                int(self._kept[record_index][row]),
                block_index,
                block["command"],
                block["commandOccurrence"],
                block["valid"],
            )
            if self.pinned
            else None
        )
        from waldo_commander.components.playback import playback

        if playback._ui_client is self.owner.scene.client:
            playback.inspect_path(self, block["line"], program=self.data["program"])

    def clear_cursor(self) -> None:
        self.pinned = False
        self.cursor = None
        if self.data is not None:
            self.owner.scene.run_method("clear_inspection", self.generation)
