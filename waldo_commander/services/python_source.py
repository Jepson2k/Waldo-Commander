"""Explicit Python insertion without rewriting the user's existing source."""

import ast
import textwrap
from typing import NamedTuple


def insert_prelude(source: str, prelude: str) -> tuple[str, int, int]:
    """Insert imports/setup after the module docstring and future imports.

    Returns ``(new_source, first_line, count)``: the 1-indexed line the
    prelude starts on and how many lines went in, a blank separator included.
    """
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
    # One blank line sets the prelude apart, unless it is imports joining the
    # imports below it or a blank line is there already.
    imports = all(
        isinstance(node, (ast.Import, ast.ImportFrom))
        for node in ast.parse(prelude).body
    )
    joins = after.startswith("\n") or (
        imports and after.startswith(("import ", "from "))
    )
    result = before + prelude.rstrip() + ("\n" if joins else "\n\n") + after
    return result, line + 1, result.count("\n") - source.count("\n")


def _bound_names(node: ast.stmt) -> set[str]:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(item.asname or item.name).split(".")[0] for item in node.names}
    if isinstance(node, ast.Assign):
        return {target.id for target in node.targets if isinstance(target, ast.Name)}
    return set()


def missing_statements(source: str, statements: list[str]) -> list[str]:
    """The *statements* the module's top level does not hold yet.

    A present statement is kept when a missing one uses a name it binds:
    missing statements go in above the existing code, where an import
    further down would come too late for them.
    """
    try:
        present = {ast.dump(node) for node in ast.parse(source).body}
    except SyntaxError:
        present = set()
    parsed = [ast.parse(statement).body[0] for statement in statements]
    keep = [ast.dump(node) not in present for node in parsed]
    for index in reversed(range(len(parsed))):
        if not keep[index]:
            continue
        used = {n.id for n in ast.walk(parsed[index]) if isinstance(n, ast.Name)}
        for earlier in range(index):
            if _bound_names(parsed[earlier]) & used:
                keep[earlier] = True
    return [statement for statement, kept in zip(statements, keep) if kept]


def _setup_load(node: ast.AST) -> tuple[str, ast.Call] | None:
    """``(variable, call)`` when *node* is ``variable = load_setup(...)``."""
    if not (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
    ):
        return None
    function = node.value.func
    name = (
        function.id
        if isinstance(function, ast.Name)
        else function.attr
        if isinstance(function, ast.Attribute)
        else None
    )
    return (node.targets[0].id, node.value) if name == "load_setup" else None


def preamble_statements(source: str, line: int | None = None) -> list[str]:
    """Module imports and literal setup loads preceding a selected line.

    Imports in the selection's enclosing blocks are added by the editor;
    setup loads inside unrelated functions or later in the file never run.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    nodes: list[ast.stmt] = []
    for node in tree.body:
        if line is not None and node.lineno >= line:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)) or (
            isinstance(node, ast.Assign) and _literal_binding(node) is not None
        ):
            nodes.append(node)
    nodes.sort(key=lambda node: (node.lineno, node.col_offset))
    return list(
        dict.fromkeys(
            textwrap.dedent(ast.get_source_segment(source, node) or "")
            for node in nodes
        )
    )


class SetupBinding(NamedTuple):
    variable: str
    name: str
    directory: str | None = None


def _cursor_tree(source: str, line: int) -> ast.Module:
    """Include an indented blank cursor line in the AST's scope spans."""
    lines = source.split("\n")
    if 0 < line <= len(lines) and not lines[line - 1].strip():
        lines[line - 1] += "pass"
    return ast.parse("\n".join(lines))


def _assignments(node: ast.AST) -> set[str]:
    """Names a statement may write, without descending into another scope."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _bound_names(node)
    names = (
        {node.id}
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
        else set()
    )
    for child in ast.iter_child_nodes(node):
        names.update(_assignments(child))
    return names


def _literal_binding(node: ast.stmt) -> SetupBinding | None:
    if (
        isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("setups.")
    ):
        parts = node.module.split(".")
        if len(parts) == 2:
            for imported in node.names:
                if imported.name == "setup":
                    return SetupBinding(imported.asname or "setup", parts[1])
    load = _setup_load(node)
    if load is None:
        return None
    variable, call = load
    if len(call.args) > 1 or any(
        k.arg not in ("name", "directory") for k in call.keywords
    ):
        return None
    keywords = {k.arg: k.value for k in call.keywords}
    name_node = call.args[0] if call.args else keywords.get("name")
    try:
        name = ast.literal_eval(name_node) if name_node is not None else None
        directory = (
            ast.literal_eval(keywords["directory"]) if "directory" in keywords else None
        )
    except (ValueError, TypeError, SyntaxError):
        return None
    if not isinstance(name, str) or (
        directory is not None and not isinstance(directory, str)
    ):
        return None
    return SetupBinding(variable, name, directory)


def program_setup(source: str, line: int | None = None) -> SetupBinding | None:
    """The literal setup binding reaching *line*, or None when ambiguous.

    A selected function has its own scope; unrelated functions never supply
    a setup. Assignments after a conditional invalidate bindings unless the
    cursor is inside that branch. Merely inspecting source executes nothing.
    """
    at = line if line and line > 0 else len(source.split("\n"))
    try:
        tree = _cursor_tree(source, at)
    except SyntaxError:
        return None
    bindings: dict[str, SetupBinding] = {}

    def forget(names: set[str]) -> None:
        for name in names:
            bindings.pop(name, None)

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if node.lineno > at:
                break
            inside = node.lineno <= at <= (node.end_lineno or node.lineno)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                forget({node.name})
                if inside:
                    local = set().union(*(_assignments(n) for n in node.body))
                    local.update(
                        a.arg
                        for a in [
                            *node.args.posonlyargs,
                            *node.args.args,
                            *node.args.kwonlyargs,
                        ]
                    )
                    if node.args.vararg:
                        local.add(node.args.vararg.arg)
                    if node.args.kwarg:
                        local.add(node.args.kwarg.arg)
                    forget(local)
                    walk(node.body)
                continue
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars:
                        forget(_assignments(item.optional_vars))
                walk(node.body)
                continue
            blocks = [getattr(node, key, []) for key in ("body", "orelse", "finalbody")]
            if isinstance(node, (ast.Try, ast.TryStar)):
                blocks += [handler.body for handler in node.handlers]
            if isinstance(node, ast.Match):
                blocks += [case.body for case in node.cases]
            if any(blocks) and not isinstance(node, ast.ClassDef):
                if inside:
                    for block in blocks:
                        if block and block[0].lineno <= at <= (
                            block[-1].end_lineno or block[-1].lineno
                        ):
                            walk(block)
                            break
                else:
                    forget(_assignments(node))
                continue
            forget(_assignments(node))
            binding = _literal_binding(node)
            if binding is not None:
                bindings[binding.variable] = binding

    walk(tree.body)
    if not bindings:
        return None
    # A skill may use an earlier, differently named setup still in scope.
    lines = source.split("\n")
    if 0 < at <= len(lines):
        try:
            selected = ast.parse(lines[at - 1].strip())
            used = {
                n.id
                for n in ast.walk(selected)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            matched = [b for name, b in bindings.items() if name in used]
            if len(matched) == 1:
                return matched[0]
            if len(matched) > 1:
                return None
        except SyntaxError:
            pass
    return next(reversed(bindings.values()))


def in_async_scope(source: str, line: int) -> bool:
    """Whether code placed below 1-indexed *line* runs inside an ``async def``.

    A function's header line counts as inside it: code inserted below a block
    opener lands in its body.
    """
    try:
        tree = _cursor_tree(source, line)
    except SyntaxError:
        return False
    innermost: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno <= line <= (node.end_lineno or node.lineno)
            and (innermost is None or node.lineno >= innermost.lineno)
        ):
            innermost = node
    return isinstance(innermost, ast.AsyncFunctionDef)


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


def delete_statement(source: str, line: int) -> str:
    """Remove the statement on the 1-indexed ``line``, every line of it.

    A block the statement leaves empty keeps a ``pass`` in its place. Raises
    ValueError when the source does not parse, no statement is on that line, or
    the statement shares a line with other code, which removing whole lines
    would take with it.
    """
    try:
        module = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(
            f"Correct the Python syntax before deleting code: {error.msg}"
        ) from error
    found: tuple[ast.stmt, list[ast.stmt]] | None = None
    for node in ast.walk(module):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for statement in block:
                if (
                    isinstance(statement, ast.stmt)
                    and statement.lineno
                    <= line
                    <= (statement.end_lineno or statement.lineno)
                    and (found is None or statement.lineno >= found[0].lineno)
                ):
                    found = statement, block
    if found is None:
        raise ValueError(f"No statement is on line {line}")
    statement, block = found
    first, last = statement.lineno, statement.end_lineno or statement.lineno
    lines = source.split("\n")
    before = lines[first - 1][: statement.col_offset]
    after = lines[last - 1][statement.end_col_offset or 0 :].strip()
    if before.strip() or (after and not after.startswith("#")):
        raise ValueError(
            f"The statement on line {line} shares its line with other code"
        )
    alone = len(block) == 1 and block is not module.body
    lines[first - 1 : last] = [before + "pass"] if alone else []
    return "\n".join(lines)
