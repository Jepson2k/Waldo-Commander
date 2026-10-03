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
def test_completions_have_required_fields() -> None:
    """Test that each completion has all required CodeMirror fields."""
    completions = command_discovery.generate_completions_from_commands()
    required_fields = {"label", "detail", "info", "apply", "type"}

    for completion in completions:
        assert isinstance(completion, dict), f"Expected dict, got {type(completion)}"
        missing = required_fields - set(completion.keys())
        assert not missing, (
            f"Completion {completion.get('label', '?')} missing fields: {missing}"
        )


@pytest.mark.unit
def test_completions_include_async_robot_client_methods() -> None:
    """Test that completions include methods from AsyncRobotClient."""
    completions = command_discovery.generate_completions_from_commands()
    completion_labels = {c["label"] for c in completions}

    expected_methods = ["home", "stop", "estop", "reset", "status"]

    for method in expected_methods:
        expected_label = f"rbt.{method}"
        assert expected_label in completion_labels, (
            f"Expected completion for {expected_label} not found"
        )


@pytest.mark.unit
def test_completions_have_function_type_for_methods() -> None:
    """Test that robot method completions have type='function'."""
    completions = command_discovery.generate_completions_from_commands()

    robot_method_completions = [
        c for c in completions if c["label"].startswith("rbt.") and c["label"] != "rbt"
    ]

    assert len(robot_method_completions) > 0, (
        "Expected at least one robot method completion"
    )

    for completion in robot_method_completions:
        assert completion["type"] == "function", (
            f"Expected type='function' for {completion['label']}, got '{completion['type']}'"
        )


@pytest.mark.unit
def test_discover_robot_commands_returns_categorized_commands() -> None:
    """Test that discover_robot_commands returns commands with categories and snippets."""
    commands = command_discovery.discover_robot_commands()

    assert isinstance(commands, dict)
    assert len(commands) > 0, "Expected at least one command"

    for name, cmd in commands.items():
        assert "title" in cmd, f"Command {name} missing 'title'"
        assert "category" in cmd, f"Command {name} missing 'category'"
        assert "snippet" in cmd, f"Command {name} missing 'snippet'"
        assert "signature" in cmd, f"Command {name} missing 'signature'"
        assert "docstring" in cmd, f"Command {name} missing 'docstring'"
        assert "rbt." in cmd["snippet"], (
            f"Snippet for {name} should contain 'rbt.', got: {cmd['snippet']}"
        )


@pytest.mark.unit
def test_excluded_methods_not_in_commands() -> None:
    """Methods without Category/Example docstrings are excluded from the palette."""
    commands = command_discovery.discover_robot_commands()
    excluded = [
        "close",
        "wait_ready",
        "stream_status",
        "stream_status_shared",
        "wait_status",
    ]
    for name in excluded:
        assert name not in commands, f"{name} should be excluded from command palette"


@pytest.mark.unit
def test_categories_from_docstrings() -> None:
    """Categories are parsed from backend docstrings, not heuristics."""
    commands = command_discovery.discover_robot_commands()
    assert commands["home"]["category"] == "Motion"
    assert commands["stop"]["category"] == "Control"
    assert commands["jog_j"]["category"] == "Jog"
    assert commands["status"]["category"] == "Query"
    assert commands["move_j"]["category"] == "Motion"


@pytest.mark.unit
def test_parse_docstring_category() -> None:
    """_parse_docstring_category extracts Category from docstrings."""
    assert (
        command_discovery._parse_docstring_category("Foo.\n\nCategory: Motion\n")
        == "Motion"
    )
    assert command_discovery._parse_docstring_category("No category here.") is None
    assert command_discovery._parse_docstring_category("  Category:  Jog \n") == "Jog"


@pytest.mark.unit
def test_completions_include_tool_methods() -> None:
    """Test that tool methods (rbt.tool.open, etc.) are discovered."""
    commands = command_discovery.discover_robot_commands()
    tool_commands = {k: v for k, v in commands.items() if k.startswith("tool.")}

    assert len(tool_commands) > 0, "Expected at least one tool command"
    assert "tool.open" in tool_commands
    assert "tool.close" in tool_commands
    assert "tool.set_position" in tool_commands

    for name, cmd in tool_commands.items():
        assert cmd["category"] == "Tool", f"{name} should have category 'Tool'"
        assert "rbt.tool." in cmd["snippet"], (
            f"{name} snippet should contain 'rbt.tool.'"
        )


@pytest.mark.unit
def test_tool_completions_have_correct_labels() -> None:
    """Test that tool completions use 'rbt.tool.X' labels in the completion list."""
    completions = command_discovery.generate_completions_from_commands()
    tool_completions = [c for c in completions if c["label"].startswith("rbt.tool.")]

    assert len(tool_completions) >= 3, "Expected at least open, close, set_position"
    labels = {c["label"] for c in tool_completions}
    assert "rbt.tool.open" in labels
    assert "rbt.tool.close" in labels
    assert "rbt.tool.set_position" in labels


@pytest.mark.unit
def test_scan_class_commands_with_prefix() -> None:
    """Test that _scan_class_commands applies the prefix correctly."""
    from waldoctl.tools import GripperTool

    commands = command_discovery._scan_class_commands(GripperTool, prefix="tool.")
    assert all(k.startswith("tool.") for k in commands), "All keys should be prefixed"
    assert all("rbt.tool." in v["title"] for v in commands.values())


@pytest.mark.unit
def test_parse_docstring_example() -> None:
    """_parse_docstring_example extracts the first indented line after Example:."""
    doc = "Foo.\n\nExample:\n    rbt.home()\n"
    assert command_discovery._parse_docstring_example(doc) == "rbt.home()"

    doc_none = "Foo.\nNo example section."
    assert command_discovery._parse_docstring_example(doc_none) is None

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
