"""Unit tests for editor completion generation."""

import pytest

from waldo_commander.profiles import get_robot
from waldo_commander.state import ui_state
import waldo_commander.services.command_discovery as _editor_mod


@pytest.fixture(autouse=True)
def _setup_robot():
    """Set up robot so command discovery can introspect AsyncRobotClient."""
    old_robot = ui_state.robot
    old_cache = _editor_mod._robot_commands_cache
    _editor_mod._robot_commands_cache = None
    ui_state.robot = get_robot()
    yield
    ui_state.robot = old_robot
    _editor_mod._robot_commands_cache = old_cache


@pytest.mark.unit
def test_completions_have_required_fields() -> None:
    """Test that each completion has all required CodeMirror fields."""
    completions = _editor_mod.generate_completions_from_commands()
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
    completions = _editor_mod.generate_completions_from_commands()
    completion_labels = {c["label"] for c in completions}

    expected_methods = ["home", "resume", "halt", "status"]

    for method in expected_methods:
        expected_label = f"rbt.{method}"
        assert expected_label in completion_labels, (
            f"Expected completion for {expected_label} not found"
        )


@pytest.mark.unit
def test_completions_have_function_type_for_methods() -> None:
    """Test that robot method completions have type='function'."""
    completions = _editor_mod.generate_completions_from_commands()

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
    commands = _editor_mod.discover_robot_commands()

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
    commands = _editor_mod.discover_robot_commands()
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
    commands = _editor_mod.discover_robot_commands()
    assert commands["home"]["category"] == "Motion"
    assert commands["resume"]["category"] == "Control"
    assert commands["jog_j"]["category"] == "Jog"
    assert commands["status"]["category"] == "Query"
    assert commands["move_j"]["category"] == "Motion"


@pytest.mark.unit
def test_parse_docstring_category() -> None:
    """_parse_docstring_category extracts Category from docstrings."""
    assert (
        _editor_mod._parse_docstring_category("Foo.\n\nCategory: Motion\n") == "Motion"
    )
    assert _editor_mod._parse_docstring_category("No category here.") is None
    assert _editor_mod._parse_docstring_category("  Category:  Jog \n") == "Jog"


@pytest.mark.unit
def test_completions_include_tool_methods() -> None:
    """Test that tool methods (rbt.tool.open, etc.) are discovered."""
    commands = _editor_mod.discover_robot_commands()
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
    completions = _editor_mod.generate_completions_from_commands()
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

    commands = _editor_mod._scan_class_commands(GripperTool, prefix="tool.")
    assert all(k.startswith("tool.") for k in commands), "All keys should be prefixed"
    assert all("rbt.tool." in v["title"] for v in commands.values())


@pytest.mark.unit
def test_parse_docstring_example() -> None:
    """_parse_docstring_example extracts the first indented line after Example:."""
    doc = "Foo.\n\nExample:\n    rbt.home()\n"
    assert _editor_mod._parse_docstring_example(doc) == "rbt.home()"

    doc_none = "Foo.\nNo example section."
    assert _editor_mod._parse_docstring_example(doc_none) is None

    doc_examples = "Foo.\n\nExamples:\n    rbt.move_j([1,2,3], speed=0.5)\n"
    assert (
        _editor_mod._parse_docstring_example(doc_examples)
        == "rbt.move_j([1,2,3], speed=0.5)"
    )
