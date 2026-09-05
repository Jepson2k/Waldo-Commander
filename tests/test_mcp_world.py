"""MCP world tools: the LLM edits the same world the GUI shows and the
backend enforces, saves and places library objects, and exports the
installation TOML."""

from __future__ import annotations

import tomllib

import pytest
from fastmcp import Client
from nicegui.testing import User

import waldoctl
from tests.helpers.mcp import payload as _payload
from tests.helpers.wait import wait_for_app_ready
from waldo_commander.mcp.server import get_mcp
from waldo_commander.services import world_files
from waldoctl import Box, Physical, Sphere


@pytest.mark.integration
async def test_world_tools_edit_the_displayed_world_and_the_library(
    user: User, tmp_path, monkeypatch
) -> None:
    from fastmcp.exceptions import ToolError

    from waldo_commander.services.control_lease import MCP, control_lease

    monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path / "lib")
    await user.open("/")
    await wait_for_app_ready()
    scene = waldoctl.commander.scene
    assert scene is not None

    mcp = get_mcp()
    try:
        async with Client(mcp) as client:
            before = _payload(await client.call_tool("world.get"))
            assert {
                "schema",
                "installation",
                "program",
                "floor_z_m",
                "confirmed",
            } <= set(before)
            assert before["schema"] == "waldo-world/1"

            wall = Box(name="wall", x=0.1, y=0.1, z=0.3, pose=(0.3, 0.0, 0.15, 0, 0, 0))
            initial = list(scene.shapes)
            with pytest.raises(ToolError, match="take_control"):
                await client.call_tool(
                    "world.set_shapes", {"shapes": [list(wall.to_wire())]}
                )
            assert scene.shapes == initial, (
                "the page's lease holder is not overridden silently"
            )
            await client.call_tool("control.take_control")
            # Start from an empty program layer whatever an earlier test left.
            assert (
                _payload(await client.call_tool("world.set_shapes", {"shapes": []}))[
                    "program"
                ]
                == []
            )
            after = _payload(
                await client.call_tool(
                    "world.set_shapes", {"shapes": [list(wall.to_wire())]}
                )
            )
            assert [s.name for s in scene.shapes] == ["wall"], (
                "the tool must reassign the scene's program layer"
            )
            assert after["program"][0][5] == "wall"
            holder = control_lease.holder()
            assert holder is not None and holder.channel == MCP

            post = Sphere(name="post", radius=0.05, pose=(0.4, 0.0, 0.05, 0, 0, 0))
            await client.call_tool("world.add_shape", {"shape": list(post.to_wire())})
            with pytest.raises(ToolError, match="already exists"):
                await client.call_tool(
                    "world.add_shape", {"shape": list(post.to_wire())}
                )
            moved = Box(
                name="wall", x=0.1, y=0.1, z=0.3, pose=(0.5, 0.0, 0.15, 0, 0, 0)
            )
            await client.call_tool(
                "world.update_shape", {"name": "wall", "shape": list(moved.to_wire())}
            )
            assert {s.name: s.pose[0] for s in scene.shapes} == {
                "wall": 0.5,
                "post": 0.4,
            }
            await client.call_tool("world.remove_shape", {"name": "post"})
            assert [s.name for s in scene.shapes] == ["wall"]
            with pytest.raises(ToolError, match="no program-layer shape"):
                await client.call_tool("world.remove_shape", {"name": "post"})
            with pytest.raises(ToolError, match="rejected"):
                await client.call_tool(
                    "world.set_shapes",
                    {"shapes": [["pyramid", [1], [0] * 6, True, None, "p", None]]},
                )
            assert [s.name for s in scene.shapes] == ["wall"], (
                "a refused edit changes nothing"
            )

            # Export / import round-trips through the one world document.
            doc = _payload(await client.call_tool("world.export"))
            await client.call_tool("world.set_shapes", {"shapes": []})
            assert scene.shapes == []
            imported = _payload(
                await client.call_tool("world.import_world", {"world": doc})
            )
            assert [s.name for s in scene.shapes] == ["wall"]
            assert imported["installation_matches"] is True

            # A library entry is a world document; one shape makes an object.
            block = Box(
                name="block",
                x=0.04,
                y=0.04,
                z=0.06,
                pose=(0.3, 0.0, 0.04, 0, 0, 0),
                physics=Physical(mass=0.05),
            )
            path = _payload(
                await client.call_tool(
                    "world.library_save",
                    {"name": "block", "shapes": [list(block.to_wire())]},
                )
            )
            assert (
                path.endswith("block.json")
                and (tmp_path / "lib" / "block.json").is_file()
            )
            assert world_files.load_entry("block").program[0].physics == Physical(
                mass=0.05
            ), "a saved object keeps its physics"
            post_entry = Sphere(
                name="post", radius=0.05, pose=(0.4, 0.0, 0.05, 0, 0, 0)
            )
            await client.call_tool(
                "world.library_save",
                {"name": "post", "shapes": [list(post_entry.to_wire())]},
            )
            assert _payload(await client.call_tool("world.library_list")) == [
                "block",
                "post",
            ]
            with pytest.raises(ToolError, match="letters, digits"):
                await client.call_tool("world.library_save", {"name": "../escape"})

            placed = _payload(
                await client.call_tool(
                    "world.place_object",
                    {
                        "entry": "post",
                        "name": "post_2",
                        "pose": [0.35, 0.1, 0.05, 0, 0, 0],
                    },
                )
            )
            by_name = {s.name: s for s in scene.shapes}
            assert by_name["post_2"].pose[:3] == (0.35, 0.1, 0.05)
            assert placed["program"][-1][5] == "post_2"
            await client.call_tool("world.library_save", {"name": "layout"})  # both
            # The backend decides what it can enforce: parol6 has no contact
            # simulation and refuses a body, and the refusal reaches the LLM.
            with pytest.raises(ToolError, match="physics"):
                await client.call_tool("world.place_object", {"entry": "block"})
            assert "block" not in {s.name for s in scene.shapes}
            with pytest.raises(ToolError, match="exactly one"):
                await client.call_tool("world.place_object", {"entry": "layout"})
            with pytest.raises(ToolError, match="unavailable"):
                await client.call_tool("world.place_object", {"entry": "nothing"})

            await client.call_tool("world.remove_shape", {"name": "post_2"})
            await client.call_tool("world.library_load", {"name": "layout"})
            assert [s.name for s in scene.shapes] == ["wall", "post_2"]
            assert _payload(
                await client.call_tool("world.library_delete", {"name": "layout"})
            ) == ["block", "post"]

            # Proposing moves a shape out of the enforced layer into the draft,
            # which the TOML export then defaults to.
            proposed = _payload(
                await client.call_tool(
                    "world.propose_installation", {"names": ["post_2"]}
                )
            )
            assert [row[5] for row in proposed["installation_draft"]] == ["post_2"]
            assert [row[5] for row in proposed["program"]] == ["wall"]
            assert [s.name for s in scene.shapes] == ["wall"]
            draft_toml = tomllib.loads(
                _payload(await client.call_tool("world.export_installation_toml"))
            )
            assert [e["name"] for e in draft_toml["installation_shapes"]] == ["post_2"]
            with pytest.raises(ToolError, match="proposal refused"):
                await client.call_tool(
                    "world.propose_installation", {"names": ["ghost"]}
                )
            cleared = _payload(
                await client.call_tool("world.discard_installation_draft")
            )
            assert cleared["installation_draft"] == []

            toml_text = _payload(
                await client.call_tool("world.export_installation_toml")
            )
            parsed = tomllib.loads(toml_text)
            assert [e["name"] for e in parsed["installation_shapes"]] == ["wall"]
            assert parsed["installation_shapes"][0]["kind"] == "box"
            assert parsed["installation_shapes"][0]["pose"][0] == 0.5
    finally:
        control_lease.reset()
        scene.shapes = []


def test_installation_toml_declares_every_field_the_config_reads():
    """The exported TOML is what the robot config parses: kind, params, pose,
    the collision flag only when off, a margin only when set, and physics as
    its own table."""
    shapes = [
        Box(name="bench", x=0.4, y=0.4, z=0.05, pose=(0.5, 0, 0.025, 0, 0, 0.1)),
        Sphere(name="marker", radius=0.02, collision=False),
        Box(
            name="stand",
            x=0.04,
            y=0.04,
            z=0.01,
            margin=0.01,
            physics=Physical(mass=None, friction=(0.8, 0.005, 0.0001)),
        ),
    ]
    parsed = tomllib.loads(world_files.installation_toml(shapes))["installation_shapes"]
    assert [e["name"] for e in parsed] == ["bench", "marker", "stand"]
    assert parsed[0]["params"] == [0.4, 0.4, 0.05] and parsed[0]["pose"][5] == 0.1
    assert "collision" not in parsed[0] and parsed[1]["collision"] is False
    assert "margin" not in parsed[0] and parsed[2]["margin"] == 0.01
    assert "physics" not in parsed[0]
    assert parsed[2]["physics"] == {"friction": [0.8, 0.005, 0.0001]}, (
        "no mass: a fixture"
    )
    assert world_files.installation_toml([]) == ""
