"""Unit tests for editor completion generation."""

import pytest
from waldoctl.setup import Frame, Parameter, Pose, SetupSnapshot
from waldoctl.signals import DigitalSignal

from waldo_commander.profiles import get_robot
from waldo_commander.state import ui_state
from waldo_commander.services import command_discovery


@pytest.fixture(autouse=True)
def _setup_robot():
    """Set up robot so command discovery can introspect AsyncRobotClient."""
    old_robot = ui_state.robot
    old_cache = command_discovery._robot_commands_cache
    command_discovery._robot_commands_cache = None
    ui_state.robot = get_robot()
    yield
    ui_state.robot = old_robot
    command_discovery._robot_commands_cache = old_cache


@pytest.mark.unit
def test_completions_offer_client_and_tool_commands_as_functions() -> None:
    """Every completion carries the fields CodeMirror reads; the client's
    methods complete as ``rbt.<name>`` and the tool's as ``rbt.tool.<name>``,
    each typed as a function."""
    completions = command_discovery.generate_completions_from_commands()
    required_fields = {"label", "detail", "info", "apply", "type"}
    for completion in completions:
        missing = required_fields - set(completion.keys())
        assert not missing, (
            f"Completion {completion.get('label', '?')} missing fields: {missing}"
        )

    labels = {c["label"] for c in completions}
    for method in ("home", "stop", "estop", "reset", "status"):
        assert f"rbt.{method}" in labels, f"rbt.{method} does not complete"
    for method in ("open", "close", "set_position"):
        assert f"rbt.tool.{method}" in labels, f"rbt.tool.{method} does not complete"

    robot_methods = [
        c for c in completions if c["label"].startswith("rbt.") and c["label"] != "rbt"
    ]
    assert robot_methods
    for completion in robot_methods:
        assert completion["type"] == "function", (
            f"Expected type='function' for {completion['label']}, got '{completion['type']}'"
        )


@pytest.mark.unit
def test_the_palette_lists_documented_commands_by_their_docstring_category() -> None:
    """The palette holds the client's and the tool's documented commands, each
    with its category, snippet and docs, the category read from the docstring;
    methods without a Category/Example docstring stay out."""
    commands = command_discovery.discover_robot_commands()
    assert commands
    for name, cmd in commands.items():
        for field in ("title", "category", "snippet", "signature", "docstring"):
            assert field in cmd, f"Command {name} missing {field!r}"
        assert "rbt." in cmd["snippet"], (
            f"Snippet for {name} should contain 'rbt.', got: {cmd['snippet']}"
        )

    for name in (
        "close",
        "wait_ready",
        "stream_status",
        "stream_status_shared",
        "wait_status",
    ):
        assert name not in commands, f"{name} should be excluded from command palette"

    assert commands["home"]["category"] == "Motion"
    assert commands["stop"]["category"] == "Control"
    assert commands["jog_j"]["category"] == "Jog"
    assert commands["status"]["category"] == "Query"
    assert commands["move_j"]["category"] == "Motion"

    tool_commands = {k: v for k, v in commands.items() if k.startswith("tool.")}
    assert {"tool.open", "tool.close", "tool.set_position"} <= tool_commands.keys()
    for name, cmd in tool_commands.items():
        assert cmd["category"] == "Tool", f"{name} should have category 'Tool'"
        assert "rbt.tool." in cmd["snippet"] and "rbt.tool." in cmd["title"], name


@pytest.mark.unit
def test_docstring_category_and_example_parsing() -> None:
    """Category and Example sections are read from docstrings, or absent."""
    assert (
        command_discovery._parse_docstring_category("Foo.\n\nCategory: Motion\n")
        == "Motion"
    )
    assert command_discovery._parse_docstring_category("No category here.") is None
    assert command_discovery._parse_docstring_category("  Category:  Jog \n") == "Jog"

    doc = "Foo.\n\nExample:\n    rbt.home()\n"
    assert command_discovery._parse_docstring_example(doc) == "rbt.home()"
    assert (
        command_discovery._parse_docstring_example("Foo.\nNo example section.") is None
    )
    doc_examples = "Foo.\n\nExamples:\n    rbt.move_j([1,2,3], speed=0.5)\n"
    assert (
        command_discovery._parse_docstring_example(doc_examples)
        == "rbt.move_j([1,2,3], speed=0.5)"
    )


@pytest.mark.unit
def test_setup_completions_read_each_entry_under_the_programs_setup_name() -> None:
    """Every setup entry completes to the Python that reads it, under the
    name the program loads the setup as; a pose is also found by its bare
    name and completes to its reference."""
    setup = SetupSnapshot(
        frames={"fixture": Frame((10, 0, 0, 0, 0, 0))},
        poses={"pick": Pose((0, 0, 5, 0, 0, 0), "fixture")},
        parameters={"clearance": Parameter(30.0, "mm")},
        signals={"grip": DigitalSignal("parol6", "output", 0, 2, 2)},
    )
    completions = command_discovery.setup_completions(setup, "cell")
    inserted = {
        c["label"]: eval(c.get("apply", c["label"]), {"cell": setup})
        for c in completions
    }
    assert inserted == {
        'cell.resolve("pick")': setup.resolve("pick"),
        "pick": setup.resolve("pick"),
        'cell.frames["fixture"]': setup.frames["fixture"],
        'cell.signals["grip"]': setup.signals["grip"],
        'cell.parameters["clearance"].value': 30.0,
    }
    details = {c["label"]: c.get("detail") for c in completions}
    assert details["pick"] == "pose in fixture"
    assert details['cell.parameters["clearance"].value'] == "30.0 mm"


@pytest.mark.unit
def test_editor_completions_do_not_execute_setup_modules(tmp_path, monkeypatch) -> None:
    from waldo_commander.components.editor import EditorPanel
    from waldo_commander.setup import SetupStore

    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    store = SetupStore(tmp_path)
    store.save("bench", SetupSnapshot(poses={"pick": Pose((1, 2, 3, 0, 0, 0))}))
    marker = tmp_path / "executed"
    path = tmp_path / "bench.py"
    path.write_text(
        path.read_text()
        + f"\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    source = f"cell = load_setup('bench', directory={str(tmp_path)!r})\nuse(cell)\n"
    binding, completions = EditorPanel._completions(source)
    assert binding[1] == "bench"
    assert any(item["label"] == 'cell.resolve("pick")' for item in completions)
    assert not marker.exists()
