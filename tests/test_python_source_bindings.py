"""Resolve the setup a skill actually reads without executing the program,
and delete a target's statement without breaking the program."""

import pytest
from waldoctl.setup import Pose, SetupSnapshot

from waldo_commander.services.python_source import (
    delete_statement,
    in_async_scope,
    preamble_statements,
    program_setup,
)
from waldo_commander.setup import SetupStore


def test_python_source_resolves_setup_and_scope_without_executing(tmp_path):
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

    # A selection's preamble ignores other scopes and later loads.
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

    # An insertion on a blank line is in async scope by that line's indentation.
    source = "async def main():\n    pass\n    \n\nprint('outside')\n"
    assert in_async_scope(source, 3)
    assert not in_async_scope(source, 4)
    assert not in_async_scope(source, 5)
    nested = "async def main():\n    def helper():\n        pass\n        \n    \n"
    assert not in_async_scope(nested, 4)
    assert in_async_scope(nested, 5)

    # Inspecting a saved setup never executes its module.
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


def test_deleting_a_targets_statement_keeps_the_program_valid():
    # Every line of a call that spans several, from any of its lines.
    call = "a = 1\nrbt.move_l([1, 2],\n    speed=0.5)\nb = 2\n"
    assert delete_statement(call, 2) == "a = 1\nb = 2\n"
    assert delete_statement(call, 3) == "a = 1\nb = 2\n"
    # A block's only statement leaves a pass behind; a block with more does not.
    loop = "for i in range(3):\n    rbt.move_l([1])\nx = 1\n"
    assert delete_statement(loop, 2) == "for i in range(3):\n    pass\nx = 1\n"
    two = "if go:\n    rbt.move_l([1])\n    rbt.move_j([2])\n"
    assert delete_statement(two, 2) == "if go:\n    rbt.move_j([2])\n"
    # The file keeps its last newline, and a form feed counts as no line break.
    assert delete_statement("a\n\x0c\nrbt.move_l([1])\nb\n", 3) == "a\n\x0c\nb\n"
    # Code sharing the statement's line would go with it, so nothing is deleted.
    for shared in ("for i in r: rbt.move_l([1])\n", "a = 1; rbt.move_l([1])\n"):
        with pytest.raises(ValueError, match="shares its line"):
            delete_statement(shared, 1)
    # The AST counts columns in UTF-8 bytes, so wide text before the end of
    # the statement must not hide what follows it.
    with pytest.raises(ValueError, match="shares its line"):
        delete_statement('f(\n    "中文中文"); x = 1\n', 1)
    # A line in a block's header would take the whole block with it.
    for header in ("@guard\ndef f():\n    pass\n", "for i in r:\n    pass\n"):
        with pytest.raises(ValueError, match="header"):
            delete_statement(header, header.count("\n") - 1)
    with pytest.raises(ValueError, match="syntax"):
        delete_statement("rbt.move_l([1\n", 1)
