"""Explicit Python insertion without rewriting the user's existing source."""

import ast


def insert_prelude(source: str, prelude: str) -> str:
    """Insert imports/setup after the module docstring and future imports."""
    try:
        module = ast.parse(source)
        ast.parse(prelude)
    except SyntaxError as error:
        raise ValueError(
            f"Correct the Python syntax before inserting code: {error.msg}"
        ) from error
    line = 0
    for index, node in enumerate(module.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            line = node.end_lineno or node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module == "__future__":
            line = node.end_lineno or node.lineno
        else:
            if line == 0:
                line = node.lineno - 1
            break
    else:
        if not module.body:
            line = len(source.splitlines())
    lines = source.splitlines(keepends=True)
    before = "".join(lines[:line])
    after = "".join(lines[line:])
    if before and not before.endswith("\n"):
        before += "\n"
    return before + prelude.rstrip() + "\n\n" + after


def loads_setup(source: str, name: str) -> bool:
    """Whether *source* may load the named setup: an import of its module, a
    ``load_setup`` call naming it, or one whose name is not a literal."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and (
            node.module == f"setups.{name}"
            or (node.module == "setups" and any(a.name == name for a in node.names))
        ):
            return True
        if isinstance(node, ast.Import) and any(
            a.name == f"setups.{name}" for a in node.names
        ):
            return True
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        called = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr
            if isinstance(function, ast.Attribute)
            else None
        )
        if called != "load_setup":
            continue
        argument = (
            node.args[0]
            if node.args
            else next((k.value for k in node.keywords if k.arg == "name"), None)
        )
        if not isinstance(argument, ast.Constant) or argument.value == name:
            return True
    return False
