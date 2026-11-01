"""Clearances against the preview checker's world, for the pose on screen."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import numpy as np
from nicegui import run, ui
from waldoctl.results import Clearance
from waldoctl.shapes import SHAPE_PREFIX

from waldo_commander.common.theme import hex_of
from waldo_commander.state import ui_state

from .config import DRAFT_PREFIX

if TYPE_CHECKING:
    from .urdf_scene import UrdfScene

logger = logging.getLogger(__name__)


class ClearanceOverlay:
    def __init__(self, view: UrdfScene) -> None:
        self.view = view
        self.enabled = False
        self.busy = False
        self.generation = 0
        self.tool_revision = 0
        self.last_started = -float("inf")
        self.key: tuple | None = None
        self.results: list[Clearance] = []
        self.timer: ui.timer | None = None
        self.button: ui.button | None = None

    def mount(self) -> None:
        scene = self.view.scene
        self.timer = ui.timer(0.1, self.update, active=False)
        scene.on("init", self.resume)
        scene.client.on_disconnect(self.suspend)

    def build_toggle(self) -> None:
        self.button = (
            ui.button(icon="straighten", on_click=self.toggle)
            .tooltip("Show clearances to keep-outs")
            .mark("scene-toggle-clearance")
            .classes("wc-clearance-toggle")
        )
        self._show_toggle()

    def _show_toggle(self) -> None:
        if self.button is not None:
            self.button.props(
                f"flat round dense color={'wc-action' if self.enabled else 'wc-text'} "
                f"aria-pressed={'true' if self.enabled else 'false'}"
            )

    def toggle(self) -> None:
        self.enabled = not self.enabled
        self._show_toggle()
        if self.enabled:
            self.resume()
        else:
            self.suspend()

    def resume(self) -> None:
        self.generation += 1
        self.key = None
        if self.timer is not None:
            self.timer.active = self.enabled

    def suspend(self) -> None:
        self.generation += 1
        if self.timer is not None:
            self.timer.active = False
        self.key = None
        self.results = []
        if self.view.scene is not None and not self.view.scene.is_deleted:
            self.view.scene.set_interaction(clearances=[])

    def dispose(self) -> None:
        self.suspend()
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def _world(self) -> tuple:
        handle = self.view._shape_handle()
        return (
            id(ui_state.active_robot),
            handle.revision,
            handle.attachment_epoch,
            self.tool_revision,
            self.view._tool_motion_last,
        )

    async def update(self) -> None:
        if (
            not self.enabled
            or self.timer is None
            or not self.timer.active
            or self.busy
            or self.view.scene.is_deleted
        ):
            return
        q = self.view._shown_q.copy()
        if not np.isfinite(q).all():
            return
        world = self._world()
        key = (tuple(q), world)
        if key != self.key:
            now = time.monotonic()
            if now - self.last_started < 0.1:
                return
            self.last_started = now
            self.busy = True
            generation = self.generation
            self.key = key
            try:
                results = await run.io_bound(ui_state.active_robot.clearances, q)
                if generation != self.generation or self.view.scene.is_deleted:
                    return
                if world != self._world():
                    self.key = None
                    self.results = []
                else:
                    self.results = results or []
            except Exception:
                logger.exception("Scene clearance query failed")
                self.results = []
            finally:
                self.busy = False
        if self.enabled and self.timer is not None and self.timer.active:
            self._draw()

    def _draw(self) -> None:
        lines = []
        for clearance in sorted(self.results, key=lambda c: c.distance):
            if not np.isfinite(clearance.distance) or clearance.distance > 0.3:
                continue
            key = clearance.obstacle
            if key.startswith(SHAPE_PREFIX) and key not in self.view._shape_objects:
                key = DRAFT_PREFIX + key.removeprefix(SHAPE_PREFIX)
            if key in self.view._hidden_objects:
                continue
            token = (
                "scene-collision"
                if clearance.distance <= 0
                else "warning"
                if clearance.distance < 0.02
                else "text-muted"
            )
            lines.append(
                {
                    "obstacle": clearance.obstacle,
                    "label": f"{round(clearance.distance * 1000)} mm",
                    "start": list(clearance.part_point),
                    "end": list(clearance.obstacle_point),
                    "color": hex_of(token),
                }
            )
            if len(lines) == 4:
                break
        self.view.scene.set_interaction(clearances=lines)
