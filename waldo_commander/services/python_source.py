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
