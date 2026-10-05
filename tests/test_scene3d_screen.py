"""The 3D view element on a page of its own: joints drawn where forward
kinematics puts them, meshes that load late taking what was sent meanwhile,
the frame cap, a hidden view, a lost WebGL context and views mounted and
removed again."""

from __future__ import annotations

import asyncio
import time
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from nicegui import app as ng_app
from nicegui import ui
from starlette.responses import Response
from urchin import URDF

from selenium.webdriver.common.action_chains import ActionChains

from tests.helpers.browser_helpers import js, run_in_app
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.scene_surface import wc_scene_js
from waldo_commander.common.theme import SceneColors, hex_of, linear_rgb
from waldo_commander.scene3d import ClipPlane, Node, WcScene

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

_PAGE = "/scene3d-test"
_page: dict[str, Any] = {}

# A chain the arms do not have: a revolute joint about a slanted axis, a
# prismatic joint sliding along -y, and a revolute joint about -z.
_SYNTHETIC_URDF = """<?xml version="1.0"?>
<robot name="synthetic">
  <link name="s0"/><link name="s1"/><link name="s2"/><link name="s3"/>
  <joint name="slant" type="revolute">
    <parent link="s0"/><child link="s1"/>
    <origin xyz="0.1 0.0 0.05" rpy="0.2 -0.1 0.3"/>
    <axis xyz="1 1 0"/><limit lower="-3" upper="3" effort="1" velocity="1"/>
  </joint>
  <joint name="slide" type="prismatic">
    <parent link="s1"/><child link="s2"/>
    <origin xyz="0.0 0.05 0.1" rpy="0.0 0.4 0.0"/>
    <axis xyz="0 -1 0"/><limit lower="-0.2" upper="0.2" effort="1" velocity="1"/>
  </joint>
  <joint name="down" type="revolute">
    <parent link="s2"/><child link="s3"/>
    <origin xyz="0.02 0.0 0.03" rpy="-0.3 0.0 0.1"/>
    <axis xyz="0 0 -1"/><limit lower="-3" upper="3" effort="1" velocity="1"/>
  </joint>
</robot>
"""

# One small triangle, for a mesh the view has to download.
_TRIANGLE_STL = (
    b"solid t\nfacet normal 0 0 1\nouter loop\n"
    b"vertex 0 0 0\nvertex 0.05 0 0\nvertex 0 0.05 0\n"
    b"endloop\nendfacet\nendsolid t\n"
)


async def _slow_stl() -> Response:
    await asyncio.sleep(0.8)
    return Response(_TRIANGLE_STL, media_type="model/stl")


def _build() -> None:
    _page["container"] = ui.element("div").classes("w-full")
    with _page["container"]:
        scene = (
            WcScene(background=hex_of("scene-bg"), reach=0.6)
            .classes("w-full")
            .style("height: 600px")
        )
        _page["menu"] = ui.context_menu().props("no-parent-event")
    with scene:
        scene.lights(1.0)
        _page["box"] = (
            scene.box(0.2, 0.2, 0.2)
            .material(hex_of("axis-x"))
            .move(0.0, 0.0, 0.1)
            .with_name("box")
        )
    scene.move_camera(1.2, 1.2, 0.9, 0.0, 0.0, 0.1, duration=0)
    scene.configure(
        inset={"anchor": "bottom-left", "margin_x": 48, "margin_y": -12},
        labels={"color": hex_of("scene-bg")},
    )
    _page["scene"] = scene


def _scene() -> WcScene:
    return _page["scene"]


def _chain(urdf: URDF, prefix: str) -> tuple[list[Node], dict[str, Node]]:
    """The URDF's links as nodes, each joint a static frame then a joint node."""
    scene = _scene()
    with scene:
        links = {
            urdf.base_link.name: scene.group().with_name(prefix + urdf.base_link.name)
        }
    joints: list[Node] = []

    def add(link: str) -> None:
        for j in urdf.joints:
            if j.parent != link:
                continue
            with links[link]:
                frame = (
                    scene.group()
                    .move(*j.origin[:3, 3])
                    .rotate_R(j.origin[:3, :3].tolist())
                )
            with frame:
                if j.joint_type == "fixed":
                    node = scene.group()
                else:
                    kind = "prismatic" if j.joint_type == "prismatic" else "revolute"
                    node = scene.joint(j.axis, kind)
                    joints.append(node)
            with node:
                links[j.child] = scene.group().with_name(prefix + j.child)
            add(j.child)

    add(urdf.base_link.name)
    return joints, links


def _world(screen: Screen, names: list[str]) -> dict[str, np.ndarray]:
    found = wc_scene_js(
        screen,
        "return Object.fromEntries(arguments[0].map(n => { const o = S.byName(n);"
        " if (!o) return [n, null]; o.updateWorldMatrix(true, false);"
        " return [n, o.matrixWorld.elements]; }))",
        names,
    )
    return {
        n: np.asarray(m).reshape((4, 4), order="F")
        for n, m in (found or {}).items()
        if m is not None
    }


_SNAP_BANDS = [[1.2, 5.0, 10.0], [0.5, 1.0, 5.0], [0.3, 0.5, 1.0], [0.0, 0.1, 0.5]]


class _Recorder:
    """What the scene hands the app for one kind of gesture."""

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []

    def admit(self, gesture: Any, args: dict[str, Any]) -> bool:
        self.log.append(("admit", gesture.drag))
        return True

    def move(self, gesture: Any, args: dict[str, Any]) -> None:
        self.log.append(("move", gesture.drag))

    def end(self, gesture: Any, args: dict[str, Any] | None, aborted: bool) -> None:
        self.log.append(("end", aborted))


def _arm() -> dict[str, Any]:
    """PAROL6's chain with a box for each link, under the page's pointer
    handling: links show their joint's ring, the last link the gizmo."""
    if "arm" in _page:
        return _page["arm"]
    scene = _scene()
    menu = _page["menu"]
    urdf = URDF.load(
        str(files("parol6") / "urdf_model/urdf/PAROL6.urdf"), lazy_load_meshes=True
    )
    _page["box"].visible(False)
    joints, links = _chain(urdf, "a:")
    actuated = [j for j in urdf.joints if j.joint_type != "fixed"]
    q = [(j.limit.lower + j.limit.upper) / 2 for j in actuated]
    sources: dict[int, str] = {}
    rings: dict[int, dict[str, Any]] = {}
    for u, j in enumerate(actuated):
        with links[j.child]:
            box = (
                scene.box(0.04, 0.04, 0.04)
                .material(hex_of("scene-arm"))
                .with_name(f"link:{j.child}")
            )
        last = u == len(actuated) - 1
        sources[box.id] = "gizmo" if last else f"ring:{u}"
        if not last:
            node = joints[u]
            rings[u] = {
                "frame": node.parent.id,
                "joint": node.id,
                "radius": 0.08 - 0.008 * u,
                "lo": j.limit.lower,
                "hi": j.limit.upper,
                "panel": [u, 1, 0],
                "name": f"J{u + 1}",
                "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            }
    scene.define_joints(joints)
    scene.set_joint_values(q)
    tcp = urdf.link_fk(cfg={j.name: v for j, v in zip(actuated, q)})[
        urdf.link_map["L6"]
    ]
    scene.set_tcp(tcp[:3, 3].tolist(), tcp[:3, :3].tolist())
    scene.move_camera(0.55, -0.55, 0.5, 0.12, 0.0, 0.2, duration=0)
    recorders = {kind: _Recorder() for kind in ("tcp", "ring", "joint", "shape")}
    scene.gestures.handlers.update(recorders)
    contexts: list[dict[str, Any]] = []

    def on_context(args: dict[str, Any]) -> None:
        contexts.append(args)
        menu.clear()
        with menu:
            ui.menu_item("hits " + ",".join(args["hits"]))
            ui.menu_item(f"at {round(args['cx'])},{round(args['cy'])}")
        scene.open_menu(args["gen"], args["cx"], args["cy"])

    scene.on_context(on_context)
    scene.set_interaction(
        sources=sources,
        rings=rings,
        rules={"available": True, "gizmo": "translate", "suspended": False},
        bands=_SNAP_BANDS,
        menu=menu.id,
        colors={
            "action": hex_of("action"),
            "muted": hex_of("text-muted"),
            "ball": SceneColors.EDIT_GRAY_HEX,
            "active": SceneColors.TCP_ACTIVE_HEX,
            "miss": SceneColors.COLLISION_HEX,
            "axisXLinear": linear_rgb("axis-x"),
            "axisYLinear": linear_rgb("axis-y"),
            "axisZLinear": linear_rgb("axis-z"),
        },
    )
    _page["arm"] = {"recorders": recorders, "contexts": contexts, "sources": sources}
    return _page["arm"]


def _rest_pointer(screen: Screen) -> None:
    screen.selenium.execute_cdp_cmd(
        "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
    )


def _mouse_to(screen: Screen, x: float, y: float) -> None:
    screen.selenium.execute_cdp_cmd(
        "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": round(x), "y": round(y)}
    )


def _touch(screen: Screen, kind: str, points: list[tuple[int, float, float]]) -> None:
    screen.selenium.execute_cdp_cmd(
        "Input.dispatchTouchEvent",
        {
            "type": kind,
            "touchPoints": [
                {"x": round(x), "y": round(y), "id": i} for i, x, y in points
            ],
        },
    )


def _handles(screen: Screen) -> list[str]:
    return wc_scene_js(screen, "return S.handles()") or []


def _show_gizmo(screen: Screen) -> tuple[float, float, float, float]:
    """Hover the last link until the gizmo shows; its X arrow's tip."""
    pixel = wait(screen, 10).until(
        lambda _: wc_scene_js(screen, "return S.hoverPixel('link:L6')")
    )
    _mouse_to(screen, *pixel)
    wait(screen, 5).until(lambda _: _handles(screen) == ["gizmo"])
    return wait(screen, 5).until(
        lambda _: wc_scene_js(screen, "return S.arrowTip('tcp:ball')")
    )


@pytest.fixture(scope="class")
def page(class_screen: Screen) -> Screen:
    ng_app.add_api_route("/test/slow.stl", _slow_stl)
    ui.page(_PAGE)(_build)
    class_screen.open(_PAGE)
    wait(class_screen, 30).until(
        lambda _: js(
            class_screen, "return !!document.querySelector('.wc-scene[data-ready]')"
        )
    )
    return class_screen


@pytest.mark.browser
class TestSceneElement:
    def test_joints_draw_where_forward_kinematics_puts_them(
        self, page: Screen, tmp_path: Path
    ) -> None:
        frames = wait(page, 10).until(lambda _: wc_scene_js(page, "return S.frames()"))
        assert frames > 0
        fetched = js(
            page,
            "return performance.getEntriesByType('resource').map(e => e.name)"
            ".filter(n => n.includes('nicegui-scene'))",
        )
        assert fetched == [], "the view loaded NiceGUI's three.js too"

        synthetic = tmp_path / "synthetic.urdf"
        synthetic.write_text(_SYNTHETIC_URDF)
        arms = {
            "p:": URDF.load(
                str(files("parol6") / "urdf_model/urdf/PAROL6.urdf"),
                lazy_load_meshes=True,
            ),
            "s:": URDF.load(str(synthetic), lazy_load_meshes=True),
        }
        built = {
            prefix: run_in_app(lambda u=urdf, p=prefix: _chain(u, p))
            for prefix, urdf in arms.items()
        }
        rng = np.random.default_rng(7)
        try:
            for _pose in range(3):
                for prefix, urdf in arms.items():
                    joints, _links = built[prefix]
                    actuated = [j for j in urdf.joints if j.joint_type != "fixed"]
                    values = [
                        float(rng.uniform(j.limit.lower, j.limit.upper) * 0.8)
                        for j in actuated
                    ]
                    scene = _scene()
                    run_in_app(lambda s=scene, nodes=joints: s.define_joints(nodes))
                    run_in_app(lambda s=scene, v=values: s.set_joint_values(v))
                    expected = {
                        prefix + link.name: matrix
                        for link, matrix in urdf.link_fk(
                            cfg={j.name: v for j, v in zip(actuated, values)}
                        ).items()
                    }

                    def drawn(_driver, e=expected) -> bool:
                        world = _world(page, list(e))
                        return len(world) == len(e) and all(
                            np.allclose(world[n], m, atol=1e-6) for n, m in e.items()
                        )

                    wait(page, 10).until(drawn, message=f"{prefix} links off their FK")
        finally:
            for _joints, links in built.values():
                root = next(iter(links.values()))
                run_in_app(root.delete)

    def test_late_meshes_take_what_was_sent_while_they_loaded(
        self, page: Screen
    ) -> None:
        scene = _scene()
        plane = ClipPlane(0.0, 0.0, 1.0, -0.01)

        def ask() -> tuple[Node, Node]:
            with scene:
                kept = scene.stl("/test/slow.stl?n=kept").with_name("kept")
                dropped = scene.stl("/test/slow.stl?n=dropped").with_name("dropped")
            return kept, dropped

        kept, dropped = run_in_app(ask)
        time.sleep(0.2)

        def meanwhile() -> None:
            kept.material(hex_of("axis-y"), 0.6).set_clipping_planes([plane]).move(
                0.3, 0, 0
            )
            scene.fx.pulse([kept])
            dropped.delete()

        run_in_app(meanwhile)
        state = """
            const o = S.byName(arguments[0]);
            if (!o) return null;
            const meshes = [];
            o.traverse(c => { if (c.isMesh) meshes.push(c); });
            const m = meshes.length ? meshes[0].material : null;
            return {meshes: meshes.length, x: o.position.x,
                    opacity: m && m.opacity,
                    color: m && '#' + m.color.getHexString(),
                    planes: m && (m.clippingPlanes || []).length};
        """
        try:
            loaded = wait(page, 10).until(
                lambda _: (s := wc_scene_js(page, state, "kept")) and s["meshes"] and s
            )
            assert loaded == {
                "meshes": 1,
                "x": pytest.approx(0.3),
                "opacity": pytest.approx(0.6),
                "color": hex_of("axis-y"),
                "planes": 1,
            }, loaded
            time.sleep(1.0)
            assert wc_scene_js(page, state, "dropped") is None
            triangles = wc_scene_js(
                page,
                "let n = 0; S.scene.traverse(o => { if (o.isMesh &&"
                " o.geometry.attributes.position.count === 3) n++; }); return n;",
            )
            assert triangles == 1, f"{triangles} triangles drawn for one kept mesh"

            # Mounted again mid-load, the view draws the mesh once, from the
            # new mount's request.
            def again() -> Node:
                with scene:
                    return scene.stl("/test/slow.stl?n=again").with_name("again")

            run_in_app(again)
            time.sleep(0.2)
            resets = wc_scene_js(page, "return S.resets")
            wc_scene_js(page, "getElement(S.root).$emit('init')")
            wait(page, 10).until(
                lambda _: wc_scene_js(page, "return S.resets") == resets + 1
            )
            wait(page, 10).until(
                lambda _: (s := wc_scene_js(page, state, "again")) and s["meshes"]
            )
            time.sleep(1.0)
            assert wc_scene_js(page, state, "again")["meshes"] == 1
        finally:

            def remove() -> None:
                for node in list(scene.objects.values()):
                    if node.name in ("kept", "again"):
                        node.delete()

            run_in_app(remove)

    def test_frames_are_capped_and_a_hidden_view_draws_none(self, page: Screen) -> None:
        drawn = page.selenium.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            "const S = getElement(document.querySelector('.wc-scene')).core.surface;"
            "const start = S.frames(); const t0 = performance.now();"
            "(function poke() { S.requestRender();"
            "  if (performance.now() - t0 < 2000) requestAnimationFrame(poke);"
            "  else done(S.frames() - start); })();"
        )
        assert 10 <= drawn <= 30 * 2 + 3, f"{drawn} frames in 2 s"

        box = _page["box"]
        wc_scene_js(page, "S.root.style.display = 'none'")
        time.sleep(0.3)
        before = wc_scene_js(page, "return S.frames()")
        try:
            run_in_app(lambda: box.move(0.0, 0.25, 0.1))
            time.sleep(0.5)
            assert wc_scene_js(page, "return S.frames()") == before, (
                "a hidden view drew"
            )
        finally:
            wc_scene_js(page, "S.root.style.display = ''")
        wait(page, 5).until(lambda _: wc_scene_js(page, "return S.frames()") > before)
        y = wc_scene_js(page, "return S.byName('box').position.y")
        run_in_app(lambda: box.move(0.0, 0.0, 0.1))
        assert y == pytest.approx(0.25)

    def test_a_lost_context_is_drawn_again(self, page: Screen) -> None:
        if not wc_scene_js(
            page, "return !!S.renderer.getContext().getExtension('WEBGL_lose_context')"
        ):
            pytest.skip("this browser cannot lose a WebGL context on request")
        pixels = """
            S.renderer.render(S.scene, S.camera);
            const gl = S.renderer.getContext();
            const r = S.canvas.getBoundingClientRect();
            const read = (px, py) => { const out = new Uint8Array(4);
              gl.readPixels(Math.round(px - r.left), Math.round(r.bottom - py), 1, 1,
                            gl.RGBA, gl.UNSIGNED_BYTE, out);
              return Array.from(out).slice(0, 3); };
            const at = S.pixelOf('box');
            return {background: read(r.right - 5, r.top + 5), box: at && read(at[0], at[1])};
        """
        background = [int(hex_of("scene-bg")[i : i + 2], 16) for i in (1, 3, 5)]
        resets = wc_scene_js(page, "return S.resets")
        wait(page, 10).until(lambda _: wc_scene_js(page, pixels)["box"])

        wc_scene_js(page, "S.renderer.forceContextLoss()")
        wait(page, 5).until(
            lambda _: js(page, "return !!document.querySelector('.wc-scene[data-gl]')")
        )
        wc_scene_js(page, "S.renderer.forceContextRestore()")
        wait(page, 5).until(
            lambda _: js(page, "return !document.querySelector('.wc-scene[data-gl]')")
        )
        seen = wait(page, 5).until(
            lambda _: (p := wc_scene_js(page, pixels))["box"] and p
        )
        assert seen["background"] == pytest.approx(background, abs=2), seen
        assert seen["box"] != pytest.approx(background, abs=8), seen
        assert wc_scene_js(page, "return S.resets") == resets, (
            "restoring resent the scene"
        )

        # Not restored, it offers a click that mounts the view again, which
        # asks for the whole scene.
        wc_scene_js(page, "S.renderer.forceContextLoss()")
        notice = page.selenium.find_element("css selector", ".wc-scene-lost")
        wait(page, 10).until(
            lambda _: notice.value_of_css_property("cursor") == "pointer"
        )
        notice.click()
        wait(page, 10).until(
            lambda _: js(
                page,
                "return !!document.querySelector('.wc-scene[data-ready]:not([data-gl])')",
            )
        )
        seen = wait(page, 10).until(
            lambda _: (p := wc_scene_js(page, pixels))["box"] and p
        )
        assert seen["background"] == pytest.approx(background, abs=2), seen

    def test_views_mounted_and_removed_leave_nothing_behind(self, page: Screen) -> None:
        js(
            page,
            "if (!window.__layout) { window.__layout = 0;"
            " const add = window.addEventListener, remove = window.removeEventListener;"
            " window.addEventListener = function (t, ...a) {"
            "   if (t === 'wc:layout') window.__layout++; return add.call(this, t, ...a); };"
            " window.removeEventListener = function (t, ...a) {"
            "   if (t === 'wc:layout') window.__layout--; return remove.call(this, t, ...a); }; }",
        )
        start = js(page, "return window.__layout")
        container = _page["container"]
        for i in range(20):

            def mount(n: int = i) -> WcScene:
                with container:
                    view = WcScene(background=hex_of("scene-bg")).style("height: 80px")
                with view:
                    view.stl(f"/test/slow.stl?n=cycle{n}")
                return view

            view = run_in_app(mount)
            wait(page, 10).until(
                lambda _: js(
                    page,
                    "return document.querySelectorAll('.wc-scene[data-ready]').length",
                )
                == 2
            )
            run_in_app(view.delete)
            wait(page, 10).until(
                lambda _: js(
                    page, "return document.querySelectorAll('.wc-scene').length"
                )
                == 1
            )
        assert js(page, "return window.__layout") == start, "removed views still listen"
        assert not wc_scene_js(
            page, "return S.renderer.getContext().isContextLost()"
        ), "removed views kept their WebGL contexts"

    def test_handle_logic(self, page: Screen) -> None:
        """The handles' rules, driven with a clock of the test's own."""
        out = page.selenium.execute_async_script(
            """
            const done = arguments[arguments.length - 1];
            Promise.all([import('wc-scene/handles.js'), import('wc-scene/gestures.js')])
            .then(([h, gs]) => {
              const out = {};
              const rad = (d) => d * Math.PI / 180;
              const at = (d) => [0.05 * Math.cos(rad(d)), 0.05 * Math.sin(rad(d))];
              // A ring drag from 170° on J2, travelling 50° below its limit.
              const panel = [1, 1, 0];
              const q0 = rad(-90);
              const drag = new h.RingDrag(q0, ...at(170), 0.05, rad(-145), rad(-50));
              const label = (step) =>
                h.ringLabel('J2', panel, drag.value(rad(step)), q0, step);
              drag.track(...at(-178)); out.seam1 = label(5);
              drag.track(...at(-173)); out.seam2 = label(5);
              drag.track(null, null); drag.track(NaN, NaN); out.ignored = label(5);
              out.finer = label(0.5); out.coarse = label(5);
              drag.track(...at(-90)); out.limit = label(5);
              drag.track(...at(-178)); drag.track(...at(-168)); out.back = label(5);
              out.atRest = h.ringLabel('J2', panel, q0, null, 5);

              // One handle at a time, with the grace.
              const seen = [];
              let rules = { gizmo: true, suspended: false };
              const allowed = (t) => !rules.suspended && (t !== 'gizmo' || rules.gizmo);
              const hover = new h.Hover({
                allowed, graceMs: 200, dragging: () => false,
                show: (t) => seen.push('+' + t), hide: (t) => seen.push('-' + t),
              });
              hover.enter('ring:1', 'mouse', 0);
              hover.leave('ring:1', 10); hover.enter('ring:2', 'mouse', 10);
              hover.tick(150); out.held = [...seen];
              hover.tick(211); out.handover = [...seen];
              hover.leave('ring:2', 300); hover.tick(450); out.graceKeeps = hover.shown;
              hover.tick(501); out.gone = hover.shown;
              hover.enter('gizmo', 'mouse', 600); out.gizmo = hover.shown;
              hover.gizmo(true, 650); hover.leave('gizmo', 650); hover.tick(900);
              out.onArrow = hover.shown;
              hover.gizmo(false, 1000); hover.tick(1201); out.offArrow = hover.shown;
              rules.gizmo = false; hover.enter('gizmo', 'mouse', 1300);
              out.hidden = hover.shown;
              rules.gizmo = true; hover.refresh(); out.revealed = hover.shown;
              hover.leave('gizmo', 1400); hover.tick(1601);
              rules.suspended = true; hover.enter('ring:1', 'mouse', 1700);
              out.suspended = hover.shown; rules.suspended = false;
              hover.tap('ring:3'); hover.leave('ring:3', 1800); hover.tick(2100);
              out.pinned = hover.shown; hover.miss(); out.unpinned = hover.shown;

              // The gizmo's marks.
              const t = h.gizmoTicks('translate', 'X', 10, 5);
              out.translateTicks = [t.points.length, t.points[11][0] - t.points[10][0],
                                    t.points.every((p) => p[1] === 0 && p[2] === 0)];
              out.translateLabel = h.gizmoLabel('translate', 'X', [0.02, 0, 0],
                [[1, 0, 0], [0, 1, 0], [0, 0, 1]], 10, 5);
              const r = h.gizmoTicks('rotate', 'Z', 10, 5);
              const radii = r.points.map((p) => Math.hypot(p[0], p[1]));
              const angles = r.points.map((p) => Math.atan2(p[1], p[0]));
              out.rotateTicks = [r.points.length, Math.max(...radii) - Math.min(...radii),
                                 angles[11] - angles[10], r.points.every((p) => p[2] === 0)];
              const c = Math.cos(rad(10)), s = Math.sin(rad(10));
              out.rotateLabel = h.gizmoLabel('rotate', 'Z', [0, 0, 0],
                [[c, -s, 0], [s, c, 0], [0, 0, 1]], 10, 5);

              // A late refusal of an earlier drag leaves the open one alone.
              const sent = [];
              const g = new gs.Gestures({ emit: (n, a) => sent.push(a) });
              const first = g.begin('ring', {}, 1, null);
              g.release(first, { delta: 5 });
              const second = g.begin('ring', {}, 1, null);
              g.reject(first.drag);
              out.secondOpen = g.current === second;
              g.release(second, {});
              done(out);
            });
            """
        )
        assert "Δ+10.0°" in out["seam1"] and "Δ+15.0°" in out["seam2"], out
        assert "Δ+15.0°" in out["ignored"], out
        assert "Δ+17.0°" in out["finer"] and "step 0.5°" in out["finer"], out
        assert "Δ+15.0°" in out["coarse"] and "step 5°" in out["coarse"], out
        assert "  \u221250.0°  " in out["limit"] and "Δ+40.0°" in out["limit"], out
        assert "Δ+20.0°" in out["back"], out
        assert out["atRest"] == "J2  \u221290.0°  step 5°", out
        assert out["held"] == ["+ring:1"], out
        assert out["handover"] == ["+ring:1", "-ring:1", "+ring:2"], out
        assert out["graceKeeps"] == "ring:2" and out["gone"] is None, out
        assert out["gizmo"] == "gizmo" and out["onArrow"] == "gizmo", out
        assert out["offArrow"] is None, out
        assert out["hidden"] is None and out["revealed"] == "gizmo", out
        assert out["suspended"] is None, out
        assert out["pinned"] == "ring:3" and out["unpinned"] is None, out
        count, spacing, on_axis = out["translateTicks"]
        assert count == 21 and spacing == pytest.approx(0.010) and on_axis, out
        assert out["translateLabel"] == "Tool X  Δ+20.0 mm  step 10 mm", out
        count, spread, spacing, flat = out["rotateTicks"]
        assert count == 21 and spread < 1e-12 and flat, out
        assert spacing == pytest.approx(np.radians(5.0)), out
        assert out["rotateLabel"] == "Tool RZ  Δ+10.0°  step 5°", out
        assert out["secondOpen"], out

    def test_a_drag_ends_once_and_belongs_to_its_finger(self, page: Screen) -> None:
        arm = run_in_app(_arm)
        tcp = arm["recorders"]["tcp"]
        try:
            # A touch drag cancelled mid-way ends aborted, once, and nothing
            # follows it.
            x, y, dx, dy = _show_gizmo(page)
            run_in_app(tcp.log.clear)
            _touch(page, "touchStart", [(1, x, y)])
            for k in range(1, 4):
                _touch(page, "touchMove", [(1, x + dx * 8 * k, y + dy * 8 * k)])
                time.sleep(0.05)
            _touch(page, "touchCancel", [])
            wait(page, 5).until(lambda _: ("end", True) in tcp.log)
            time.sleep(0.8)
            ends = [e for e in tcp.log if e[0] == "end"]
            assert ends == [("end", True)], tcp.log
            assert tcp.log[0][0] == "admit" and tcp.log[-1] == ("end", True), tcp.log

            # A second finger neither moves nor ends the first one's drag.
            spot = wait(page, 5).until(
                lambda _: wc_scene_js(page, "return S.emptySpot()")
            )
            x, y, dx, dy = _show_gizmo(page)
            run_in_app(tcp.log.clear)
            _touch(page, "touchStart", [(1, x, y)])
            _touch(page, "touchMove", [(1, x + dx * 8, y + dy * 8)])
            _touch(page, "touchStart", [(1, x + dx * 8, y + dy * 8), (2, *spot)])
            _touch(
                page,
                "touchMove",
                [(1, x + dx * 8, y + dy * 8), (2, spot[0] + 40, spot[1])],
            )
            # Lifting the second finger: the touch points still down.
            _touch(page, "touchMove", [(1, x + dx * 8, y + dy * 8)])
            time.sleep(0.3)
            assert not [e for e in tcp.log if e[0] == "end"], tcp.log
            _touch(page, "touchMove", [(1, x + dx * 16, y + dy * 16)])
            _touch(page, "touchEnd", [])
            wait(page, 5).until(lambda _: ("end", False) in tcp.log)
            assert [e for e in tcp.log if e[0] == "admit"] == [tcp.log[0]], tcp.log
            assert [e for e in tcp.log if e[0] == "end"] == [("end", False)], tcp.log

            # A context lost mid-drag ends it aborted; once drawn again, a new
            # drag works.
            x, y, dx, dy = _show_gizmo(page)
            run_in_app(tcp.log.clear)
            actions = ActionChains(page.selenium, duration=20)
            actions.move_by_offset(0, 0)
            actions.perform()
            page.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": round(x),
                    "y": round(y),
                    "button": "left",
                    "clickCount": 1,
                },
            )
            _mouse_to(page, x + dx * 10, y + dy * 10)
            wc_scene_js(page, "S.renderer.forceContextLoss()")
            wait(page, 5).until(lambda _: ("end", True) in tcp.log)
            page.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": round(x + dx * 10),
                    "y": round(y + dy * 10),
                    "button": "left",
                    "clickCount": 1,
                },
            )
            wc_scene_js(page, "S.renderer.forceContextRestore()")
            wait(page, 5).until(
                lambda _: js(
                    page, "return !document.querySelector('.wc-scene[data-gl]')"
                )
            )
            x, y, dx, dy = _show_gizmo(page)
            run_in_app(tcp.log.clear)
            page.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": round(x),
                    "y": round(y),
                    "button": "left",
                    "clickCount": 1,
                },
            )
            _mouse_to(page, x + dx * 10, y + dy * 10)
            page.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": round(x + dx * 10),
                    "y": round(y + dy * 10),
                    "button": "left",
                    "clickCount": 1,
                },
            )
            wait(page, 5).until(lambda _: ("end", False) in tcp.log)
        finally:
            _rest_pointer(page)

    def test_hidden_handles_take_no_press(self, page: Screen) -> None:
        arm = run_in_app(_arm)
        tcp, ring = arm["recorders"]["tcp"], arm["recorders"]["ring"]
        scene = _scene()
        try:
            x, y, _dx, _dy = _show_gizmo(page)
            run_in_app(
                lambda: scene.set_interaction(
                    rules={"available": True, "gizmo": "hidden", "suspended": False}
                )
            )
            wait(page, 5).until(lambda _: _handles(page) == [])
            run_in_app(tcp.log.clear)
            for kind in ("mousePressed", "mouseReleased"):
                page.selenium.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": kind,
                        "x": round(x),
                        "y": round(y),
                        "button": "left",
                        "clickCount": 1,
                    },
                )
            time.sleep(0.3)
            assert tcp.log == [], "a hidden gizmo took a press"
        finally:
            run_in_app(
                lambda: scene.set_interaction(
                    rules={"available": True, "gizmo": "translate", "suspended": False}
                )
            )
        # A ring that went with the pointer takes no press where it was.
        pixel = wait(page, 10).until(
            lambda _: wc_scene_js(page, "return S.hoverPixel('link:L2')")
        )
        _mouse_to(page, *pixel)
        wait(page, 5).until(lambda _: _handles(page) == ["ring:1"])
        dial = wc_scene_js(page, "return S.dial(1)")
        (angle,) = wait(page, 5).until(
            lambda _: (a := wc_scene_js(page, "return S.ringGrab(1)")) is not None
            and [a]
        )
        t = dial["q"] + np.radians(angle)
        grab = wc_scene_js(
            page,
            "return S.project('jog:dial:1', [arguments[0]])[0]",
            [dial["r"] * np.cos(t), dial["r"] * np.sin(t), 0.0],
        )
        _rest_pointer(page)
        wait(page, 5).until(lambda _: _handles(page) == [])
        run_in_app(ring.log.clear)
        for kind in ("mousePressed", "mouseReleased"):
            page.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": kind,
                    "x": round(grab[0]),
                    "y": round(grab[1]),
                    "button": "left",
                    "clickCount": 1,
                },
            )
        time.sleep(0.3)
        assert ring.log == [], "a hidden ring took a press"
        _rest_pointer(page)

    def test_the_menu_opens_once_at_the_pointer_for_each_way_of_asking(
        self, page: Screen
    ) -> None:
        arm = run_in_app(_arm)
        contexts = arm["contexts"]
        items = (
            "return [...document.querySelectorAll('.q-menu')]"
            ".filter(m => m.getClientRects().length > 0)"
            ".flatMap(m => [...m.querySelectorAll('.q-item')].map(i => i.textContent.trim()))"
        )
        menu_box = (
            "const m = [...document.querySelectorAll('.q-menu')]"
            ".find(m => m.getClientRects().length > 0);"
            "return m && m.getBoundingClientRect().toJSON();"
        )

        def right_click(x: float, y: float) -> None:
            for kind in ("mousePressed", "mouseReleased"):
                page.selenium.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": kind,
                        "x": round(x),
                        "y": round(y),
                        "button": "right",
                        "clickCount": 1,
                    },
                )

        def synthetic(script: str, x: float, y: float) -> None:
            wc_scene_js(page, script, x, y)

        spot = wait(page, 5).until(lambda _: wc_scene_js(page, "return S.emptySpot()"))
        x, y = spot

        # A right-click, as this browser sends it: one request, the menu at
        # the pointer, filled.
        n = len(contexts)
        right_click(x, y)
        shown = wait(page, 5).until(lambda _: (i := js(page, items)) and i)
        assert f"at {round(x)},{round(y)}" in shown, shown
        assert len(contexts) == n + 1

        # Again elsewhere, off the open menu: it opens there, with what that
        # click found.
        x2, y2 = x - 90, y - 70
        right_click(x2, y2)
        wait(page, 5).until(
            lambda _: f"at {round(x2)},{round(y2)}" in (js(page, items) or [])
        )
        box = js(page, menu_box)
        assert abs(box["left"] - x2) < 40 and abs(box["top"] - y2) < 40, box

        # A click on the scene closes it.
        for kind in ("mousePressed", "mouseReleased"):
            page.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": kind,
                    "x": round(x),
                    "y": round(y),
                    "button": "left",
                    "clickCount": 1,
                },
            )
        wait(page, 5).until(lambda _: no_visible(page, ".q-menu"))

        # Windows' order: the contextmenu event after the release.
        n = len(contexts)
        synthetic(
            "const o = {clientX: arguments[0], clientY: arguments[1], bubbles: true,"
            " cancelable: true, button: 2, pointerId: 9, pointerType: 'mouse'};"
            "S.canvas.dispatchEvent(new PointerEvent('pointerdown', o));"
            "S.canvas.dispatchEvent(new PointerEvent('pointerup', o));"
            "S.canvas.dispatchEvent(new MouseEvent('contextmenu', o));",
            x,
            y,
        )
        wait(page, 5).until(lambda _: js(page, items))
        time.sleep(0.2)
        assert len(contexts) == n + 1, contexts[n:]

        # A right-drag with the menu open: it closes and asks for nothing.
        n = len(contexts)
        camera = wc_scene_js(page, "return S.cameraPose()")
        page.selenium.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": round(x),
                "y": round(y),
                "button": "right",
                "clickCount": 1,
            },
        )
        for k in range(1, 7):
            page.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": round(x) + 10 * k,
                    "y": round(y),
                    "button": "right",
                },
            )
        page.selenium.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": round(x) + 60,
                "y": round(y),
                "button": "right",
                "clickCount": 1,
            },
        )
        wait(page, 5).until(lambda _: no_visible(page, ".q-menu"))
        time.sleep(0.3)
        assert len(contexts) == n and no_visible(page, ".q-menu")
        wc_scene_js(page, "S.setCameraPose(arguments[0])", camera)

        # macOS's ctrl+click: the menu, and no orbit.
        page.selenium.execute_cdp_cmd(
            "Emulation.setUserAgentOverride",
            {
                "userAgent": page.selenium.execute_script("return navigator.userAgent"),
                "platform": "MacIntel",
            },
        )
        try:
            n = len(contexts)
            synthetic(
                "const o = {clientX: arguments[0], clientY: arguments[1], bubbles: true,"
                " cancelable: true, button: 0, ctrlKey: true, pointerId: 10, pointerType: 'mouse'};"
                "S.canvas.dispatchEvent(new PointerEvent('pointerdown', o));"
                "S.canvas.dispatchEvent(new MouseEvent('contextmenu', o));"
                "S.canvas.dispatchEvent(new PointerEvent('pointermove', {...o, clientX: o.clientX + 40}));"
                "S.canvas.dispatchEvent(new PointerEvent('pointerup', {...o, clientX: o.clientX + 40}));",
                x,
                y,
            )
            wait(page, 5).until(lambda _: len(contexts) == n + 1)
            assert wc_scene_js(page, "return S.cameraPose()") == pytest.approx(camera)
        finally:
            page.selenium.execute_cdp_cmd(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": page.selenium.execute_script(
                        "return navigator.userAgent"
                    )
                },
            )
        wc_scene_js(
            page, "getElement(" + str(run_in_app(lambda: _page["menu"].id)) + ").hide()"
        )
        wait(page, 5).until(lambda _: no_visible(page, ".q-menu"))

        # A touch held still opens it, and is no tap.
        n = len(contexts)
        _touch(page, "touchStart", [(3, x, y)])
        time.sleep(0.7)
        _touch(page, "touchEnd", [])
        wait(page, 5).until(lambda _: len(contexts) == n + 1)
        wait(page, 5).until(lambda _: js(page, items))
