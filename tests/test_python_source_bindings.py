"""Resolve the setup a skill actually reads without executing the program."""

from waldo_commander.services.python_source import (
    in_async_scope,
    preamble_statements,
    program_setup,
)


def test_setup_binding_follows_scope_reassignment_and_directory():
    source = (
        "from waldo_commander.setup import load_setup\n"
        "setup = load_setup('first')\n"
        "def unused():\n"
        "    hidden = load_setup('hidden')\n"
        "setup = load_setup('second', directory='/other/cell')\n"
        "use(setup)\n"
    )
    assert program_setup(source)[1] == "second"
    binding = program_setup(source, 6)
    assert binding.directory == "/other/cell"
    assert program_setup(source, 2).name == "first"
    assert program_setup("def unused():\n    setup = load_setup('hidden')\n") is None
    assert program_setup(source + "setup = other()\nuse(setup)\n") is None
    assert program_setup("setup = load_setup('bench', directory=chosen)\n") is None
    assert (
        program_setup("from setups.bench import setup as cell\nuse(cell)\n").variable
        == "cell"
    )
    conditional = "setup = load_setup('first')\nif flag:\n    setup = load_setup('maybe')\nuse(setup)\n"
    assert program_setup(conditional, 5) is None
    assert program_setup(conditional, 3).name == "maybe"


def test_selection_preamble_ignores_other_scopes_and_later_loads():
    source = (
        "from waldo_commander.setup import load_setup\n"
        "setup = load_setup('first')\n"
        "def unused():\n"
        "    setup = load_setup('hidden')\n"
        "use(setup)\n"
        "setup = load_setup('later')\n"
    )
    preamble = "\n".join(preamble_statements(source, 5))
    assert "first" in preamble
    assert "hidden" not in preamble and "later" not in preamble


def test_async_insertion_on_blank_lines_uses_the_lines_indentation():
    source = "async def main():\n    pass\n    \n\nprint('outside')\n"
    assert in_async_scope(source, 3)
    assert not in_async_scope(source, 4)
    assert not in_async_scope(source, 5)
    nested = "async def main():\n    def helper():\n        pass\n        \n    \n"
    assert not in_async_scope(nested, 4)
    assert in_async_scope(nested, 5)


def test_setup_inspection_never_executes_the_module(tmp_path):
    from waldoctl.setup import SetupSnapshot, Pose
    from waldo_commander.setup import SetupStore

    store = SetupStore(tmp_path)
    snapshot = SetupSnapshot().with_pose("pick", Pose((1, 2, 3, 0, 0, 0)))
    store.save("bench", snapshot)
    path = tmp_path / "bench.py"
    marker = tmp_path / "executed"
    path.write_text(
        path.read_text()
        + f"\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    assert store.read_literal("bench").resolve("pick") == snapshot.resolve("pick")
    assert not marker.exists()
