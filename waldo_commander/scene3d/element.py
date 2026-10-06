"""The 3D view: a three.js scene the app draws into through ``Node`` objects.

Python keeps the scene's state (``objects``, the joint values, the camera,
the view settings) and the browser draws it. Changes made in one pass of the
event loop go out as one message once the browser has mounted the view; when
it mounts, or mounts again, it is sent the whole scene.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from nicegui import ui
from nicegui.dependencies import register_esm

from .interaction import Gestures
from .node import ClipPlane, Node, current_parent
from .protocol import fixed, fixed_all, quaternion

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent


def _register(name: str, directory: Path) -> None:
    # A directory's cache key follows its own mtime, which an edit to a file
    # inside it does not change.
    newest = max(p.stat().st_mtime for p in directory.rglob("*.js"))
    register_esm(name, directory, max_time=newest)


_register("three", _HERE / "vendor" / "three")
_register("wc-scene", _HERE / "js")

DEFAULT_CONFIG: dict[str, Any] = {
    "background": None,
    "reach": 1.0,
    "max_fps": 30,
    "raycast_threshold": 0.005,
    "glow": {"color": None, "opacity": 0.2, "scale": 1.5},
    "inset": None,
    "labels": None,
}


class WcScene(ui.element, component="wc_scene.js", default_classes="wc-scene"):
    """The 3D view."""

    def __init__(self, **config: Any) -> None:
        super().__init__()
        self._ids = itertools.count()
        self.objects: dict[int, Node] = {}
        self.root = Node(self, "root", [], None)
        self.live = False
        self.config: dict[str, Any] = {**DEFAULT_CONFIG, **config}
        self._config_changes: dict[str, Any] = {}
        self._created: dict[int, Node] = {}
        self._removed: list[int] = []
        self._dirty: dict[int, int] = {}
        self._joints: list[Node] = []
        self._joints_changed = False
        self._joint_values_changed: set[int] = set()
        self._camera: list[float] | None = None
        self._commands: list[Callable[[], list[Any] | None]] = []
        self._flush_handle: asyncio.Handle | None = None
        self._loop = asyncio.get_running_loop()
        self._token: list[Any] = []
        self.fx = Effects(self)
        self.gestures = Gestures(self)
        self._interaction: dict[str, Any] = {}
        self._tcp_rev = 0
        self._tcp_history: dict[int, tuple[list[float], list[list[float]]]] = {}
        self._context_handler: Callable[[dict[str, Any]], None] | None = None
        self.on("init", self._handle_init)
        self.on("gesture", lambda e: self.gestures.handle(e.args))
        self.on("context", self._handle_context)
        self.client.on_disconnect(self._handle_disconnect)

    # -- parenting ----------------------------------------------------------

    def __enter__(self) -> WcScene:  # type: ignore[override]
        self._token.append(current_parent.set(self.root))
        return self

    def __exit__(self, *_: object) -> None:  # type: ignore[override]
        current_parent.reset(self._token.pop())

    def _parent(self) -> Node:
        parent = current_parent.get()
        return parent if parent is not None and parent.scene is self else self.root

    def _new_id(self) -> int:
        return next(self._ids)

    # -- objects ------------------------------------------------------------

    def group(self) -> Node:
        return Node(self, "group", [], self._parent())

    def box(
        self,
        width: float = 1.0,
        height: float = 1.0,
        depth: float = 1.0,
        wireframe: bool = False,
    ) -> Node:
        return Node(
            self, "box", [width, height, depth], self._parent(), wireframe=wireframe
        )

    def sphere(
        self,
        radius: float = 1.0,
        width_segments: int = 32,
        height_segments: int = 16,
        wireframe: bool = False,
    ) -> Node:
        return Node(
            self,
            "sphere",
            [radius, width_segments, height_segments],
            self._parent(),
            wireframe=wireframe,
        )

    def cylinder(
        self,
        top_radius: float = 1.0,
        bottom_radius: float = 1.0,
        height: float = 1.0,
        radial_segments: int = 8,
        height_segments: int = 1,
        wireframe: bool = False,
    ) -> Node:
        return Node(
            self,
            "cylinder",
            [top_radius, bottom_radius, height, radial_segments, height_segments],
            self._parent(),
            wireframe=wireframe,
        )

    def capsule(
        self,
        radius: float = 1.0,
        length: float = 1.0,
        cap_segments: int = 4,
        radial_segments: int = 8,
        height_segments: int = 1,
        wireframe: bool = False,
    ) -> Node:
        return Node(
            self,
            "capsule",
            [radius, length, cap_segments, radial_segments, height_segments],
            self._parent(),
            wireframe=wireframe,
        )

    def lathe(
        self,
        points: list[list[float]],
        segments: int = 12,
        phi_start: float = 0.0,
        phi_length: float = 2 * math.pi,
        wireframe: bool = False,
    ) -> Node:
        return Node(
            self,
            "lathe",
            [points, segments, phi_start, phi_length],
            self._parent(),
            wireframe=wireframe,
        )

    def line(self, start: list[float], end: list[float]) -> Node:
        return Node(self, "line", [list(start), list(end)], self._parent())

    def polyline(
        self,
        points: list[list[float]],
        colors: list[list[float]] | None = None,
        dashed: bool = False,
        dash_size: float = 3.0,
        gap_size: float = 1.0,
    ) -> Node:
        """A line through *points*; *colors* are per-point RGB floats, passed as they are."""
        if len(points) < 2:
            raise ValueError(f"a polyline needs 2 points (got {len(points)})")
        if colors is not None and len(colors) != len(points):
            raise ValueError(f"{len(colors)} colours for {len(points)} points")
        return Node(
            self,
            "polyline",
            [[fixed_all(p) for p in points], colors, dashed, dash_size, gap_size],
            self._parent(),
        )

    def stl(self, url: str, wireframe: bool = False) -> Node:
        """A mesh loaded from *url*: shaded with shadows, or its edges with *wireframe*."""
        return Node(self, "stl", [url], self._parent(), wireframe=wireframe)

    def floor(
        self, reach: float, sectors: int, rings: int, color: str, grid_color: str
    ) -> Node:
        """A floor disc fading out past *reach*, under a polar grid that fades with radius."""
        return Node(
            self, "floor", [reach, sectors, rings, color, grid_color], self._parent()
        )

    def lights(self, radius: float) -> Node:
        """Ambient, key, fill and rim lights; the key casts shadows within *radius*."""
        return Node(self, "lights", [radius], self._parent())

    def joint(
        self, axis: Sequence[float], kind: Literal["revolute", "prismatic"]
    ) -> Node:
        """A frame turned about (or slid along) *axis* by its joint value."""
        length = math.sqrt(sum(float(a) ** 2 for a in axis))
        if not length:
            raise ValueError("a joint axis has no direction")
        return Node(
            self, "joint", [fixed_all(a / length for a in axis), kind], self._parent()
        )

    # -- joints -------------------------------------------------------------

    def define_joints(self, joints: list[Node]) -> None:
        """The joints ``set_joint_values`` sets, in its order."""
        self._own()
        self._joints = list(joints)
        self._joints_changed = True
        self._schedule()

    def set_joint_values(self, values: Sequence[float]) -> None:
        """Set each joint's value (radians, or metres when prismatic).

        A non-finite value leaves its joint where it is.
        """
        self._own()
        for index, (joint, value) in enumerate(zip(self._joints, values, strict=False)):
            value = float(value)
            if not math.isfinite(value):
                logger.warning("joint %d: non-finite value %r dropped", index, value)
                continue
            if value != joint.q:
                joint.q = value
                self._joint_values_changed.add(index)
        if self._joint_values_changed:
            self._schedule()

    def resend_joints(self) -> None:
        """Send every joint value again, over whatever the browser shows."""
        self._own()
        self._joint_values_changed.update(range(len(self._joints)))
        self._schedule()

    # -- view ---------------------------------------------------------------

    def configure(self, **changes: Any) -> None:
        """Change view settings: background, reach (sets the fog), max_fps,
        raycast_threshold, glow, inset, labels."""
        self._own()
        for key, value in changes.items():
            if key not in DEFAULT_CONFIG:
                raise KeyError(f"unknown scene setting {key!r}")
            if self.config[key] != value:
                self.config[key] = value
                self._config_changes[key] = value
        if self._config_changes:
            self._schedule()

    def move_camera(
        self,
        x: float,
        y: float,
        z: float,
        look_at_x: float = 0.0,
        look_at_y: float = 0.0,
        look_at_z: float = 0.0,
        duration: float = 0.5,
        ease: bool = False,
    ) -> None:
        """Move the camera over *duration* seconds; *ease* starts and ends it gently."""
        pose = fixed_all((x, y, z, look_at_x, look_at_y, look_at_z))
        self._camera = pose
        self._command(lambda: ["cam", pose, float(duration), bool(ease)])

    # -- interaction --------------------------------------------------------

    def set_interaction(self, **changes: Any) -> None:
        """What the browser's pointer handling needs: hover sources, rings,
        handle rules, edit and keep-out move state, colours and snap bands."""
        self._own()
        changed = {k: v for k, v in changes.items() if self._interaction.get(k) != v}
        if not changed:
            return
        self._interaction.update(changed)
        self._command(lambda: ["ix", changed])

    @property
    def interaction(self) -> dict[str, Any]:
        return dict(self._interaction)

    def set_tcp(self, position: Sequence[float], R: Sequence[Sequence[float]]) -> int:
        """Place the gizmo's frame on the TCP; returns the placement's revision."""
        self._own()
        self._tcp_rev += 1
        self._tcp_history[self._tcp_rev] = (
            [float(v) for v in position],
            [list(map(float, r)) for r in R],
        )
        self._tcp_history.pop(self._tcp_rev - 64, None)
        rev = self._tcp_rev
        self._command(lambda: self._tcp_op(rev))
        return rev

    def tcp_at(self, rev: Any) -> tuple[list[float], list[list[float]]] | None:
        """The TCP placement the browser showed at revision *rev*, if recent."""
        return self._tcp_history.get(rev) if isinstance(rev, int) else None

    def _tcp_op(self, rev: int) -> list[Any] | None:
        if rev != self._tcp_rev:
            return None
        p, R = self._tcp_history[rev]
        return ["tcp", rev, fixed_all(p), quaternion(R)]

    def set_gizmo_miss(self, missed: bool) -> None:
        """Whether the drag's last pose was out of reach: its spring back is tinted."""
        self._command(lambda: ["miss", int(missed)])

    def on_context(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Call *handler* with a right-click's ``hits``, ``ground`` and request ``gen``."""
        self._context_handler = handler

    def open_menu(self, gen: int, cx: float, cy: float) -> None:
        """Open the context menu at viewport point (cx, cy) for request *gen*."""
        self._command(lambda: ["menu", gen, float(cx), float(cy)])

    def _handle_context(self, e: Any) -> None:
        args = e.args if isinstance(e.args, dict) else {}
        authorized = self.gestures.authorized()
        if args.get("epoch") != self.gestures.epoch or not authorized:
            return
        if self._context_handler is not None:
            self._context_handler(args)

    def _handle_disconnect(self) -> None:
        if not self.is_deleted:
            self.gestures.bump()

    def _command(self, build: Callable[[], list[Any] | None]) -> None:
        """Queue an op built at send time; a build that returns None is dropped."""
        self._own()
        if not self.live:
            return
        self._commands.append(build)
        self._schedule()

    # -- change tracking ----------------------------------------------------

    def _own(self) -> None:
        """Refuse a change from outside the loop that runs the scene. A scene
        whose loop has stopped is drawn nowhere, so what it is told is moot."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not self._loop and self._loop.is_running():
            raise RuntimeError("the 3D scene changed outside its event loop")

    def _created_node(self, node: Node) -> None:
        self._own()
        self.objects[node.id] = node
        self._created[node.id] = node
        self._schedule()

    def _deleted_node(self, node: Node) -> None:
        self._own()
        unsent = node.id in self._created
        removed: list[Node] = []

        def collect(n: Node) -> None:
            removed.append(n)
            for child in n.children:
                collect(child)

        collect(node)
        if node.parent is not None and node in node.parent.children:
            node.parent.children.remove(node)
        for n in removed:
            self._created.pop(n.id, None)
            self._dirty.pop(n.id, None)
        node._forget()
        if not unsent:
            self._removed.append(node.id)
        self._schedule()

    def _changed(self, node: Node, bits: int) -> None:
        self._own()
        if node.id in self._created:
            return
        self._dirty[node.id] = self._dirty.get(node.id, 0) | bits
        self._schedule()

    def _schedule(self) -> None:
        if self.live and self._flush_handle is None and not self._loop.is_closed():
            self._flush_handle = self._loop.call_soon(self._flush)

    # -- wire ---------------------------------------------------------------

    def _flush(self) -> None:
        self._flush_handle = None
        if not self.live or self.is_deleted:
            return
        ops: list[list[Any]] = [["d", nid] for nid in self._removed]
        ops += [node._create_op() for node in self._created.values()]
        for nid, bits in self._dirty.items():
            node = self.objects.get(nid)
            if node is not None:
                ops.append(node._update_op(bits))
        ops += self._joint_ops(full=False)
        if self._config_changes:
            ops.append(["cfg", self._config_changes])
        for build in self._commands:
            op = build()
            if op is not None:
                ops.append(op)
        self._clear_changes()
        if ops:
            self._send(ops)

    def _joint_ops(self, *, full: bool) -> list[list[Any]]:
        ops: list[list[Any]] = []
        if full or self._joints_changed:
            ops.append(["J", [j.id for j in self._joints]])
        if full or self._joints_changed:
            values = [fixed(j.q) for j in self._joints]
        elif self._joint_values_changed:
            values = [
                fixed(j.q) if i in self._joint_values_changed else None
                for i, j in enumerate(self._joints)
            ]
            while values and values[-1] is None:
                values.pop()
        else:
            return ops
        if values:
            ops.append(["q", values])
        return ops

    def _clear_changes(self) -> None:
        self._created.clear()
        self._removed.clear()
        self._dirty.clear()
        self._joints_changed = False
        self._joint_values_changed.clear()
        self._config_changes = {}
        self._commands.clear()

    def _snapshot(self) -> list[list[Any]]:
        ops: list[list[Any]] = [["reset"], ["cfg", self.config]]

        def walk(node: Node) -> None:
            for child in node.children:
                ops.append(child._create_op())
                walk(child)

        walk(self.root)
        ops += self._joint_ops(full=True)
        if self._camera is not None:
            ops.append(["cam", self._camera, 0.0, False])
        ops.append(["epoch", self.gestures.epoch])
        if self._interaction:
            ops.append(["ix", self._interaction])
        if self._tcp_rev:
            ops.append(self._tcp_op(self._tcp_rev))
        return ops

    def _handle_init(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self.live = True
        self.gestures.epoch += 1
        self.gestures.abort_all()
        self._clear_changes()
        self._send(self._snapshot())

    def _send(self, ops: list[list[Any]]) -> None:
        payload = json.dumps(ops, separators=(",", ":"), allow_nan=False)
        self.client.run_javascript(f"runMethod({self.id},'apply',[{payload}])")

    def _handle_delete(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self.gestures.abort_all()
        self.gestures.cancel_watch()
        self.live = False
        super()._handle_delete()


class Effects:
    """Short animations of nodes, run by the browser. A node deleted before
    the effect is sent is left out of it."""

    def __init__(self, scene: WcScene) -> None:
        self._scene = scene

    def _run(self, name: str, nodes: Sequence[Node], *args: Any) -> None:
        nodes = list(nodes)

        def build() -> list[Any] | None:
            ids = [n.id for n in nodes if not n.deleted]
            return ["fx", name, ids, *args] if ids else None

        self._scene._command(build)

    def reveal(
        self, segments: Sequence[Sequence[Node]], markers: Sequence[Node]
    ) -> None:
        """Draw paths in and pop markers in. Each segment is its line, then
        its direction cones."""
        segments = [list(s) for s in segments]
        markers = list(markers)

        def build() -> list[Any] | None:
            lines = [[n.id for n in s if not n.deleted] for s in segments]
            lines = [s for s in lines if s]
            dots = [n.id for n in markers if not n.deleted]
            return ["fx", "reveal", lines, dots] if lines or dots else None

        self._scene._command(build)

    def flash(self, nodes: Sequence[Node]) -> None:
        """Glow and pop, e.g. a tool just mounted."""
        self._run("flash", nodes)

    def alarm(self, nodes: Sequence[Node], color: str) -> None:
        """Two hard flashes in *color*, e.g. on geometry that just collided."""
        self._run("alarm", nodes, color)

    def fade_in(self, nodes: Sequence[Node], ms: float = 450) -> None:
        """Fade from clear to each node's own opacity."""
        self._run("fade", nodes, ms)

    def pulse(self, nodes: Sequence[Node]) -> None:
        """Ripple *nodes* in order, replacing any ripple running; none stops it."""
        nodes = list(nodes)
        self._scene._command(
            lambda: ["fx", "pulse", [n.id for n in nodes if not n.deleted]]
        )


__all__ = ["ClipPlane", "Effects", "Node", "WcScene"]
