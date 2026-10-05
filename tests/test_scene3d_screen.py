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

from tests.helpers.browser_helpers import js, run_in_app
from tests.helpers.browser_session import wait
from tests.helpers.scene_surface import wc_scene_js
from waldo_commander.common.theme import hex_of
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
