"""Source spans for motion targets that can be edited without changing their meaning."""

import ast
import math
from dataclasses import dataclass
from typing import Literal
from functools import lru_cache

from waldo_commander.services.programs import insert_below_line


def insertion_line(source: str, cursor: int) -> int:
    """The line scene code goes below for an editor *cursor*: past the end of
    a statement or block header the cursor is inside, or below the last
    statement when the cursor is unset (0)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(
            f"Choose a complete statement in a valid program before inserting: {error.msg}"
        ) from error
    statements = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
    if not cursor:
        return max((n.end_lineno or n.lineno for n in statements), default=0)
    line = cursor
    for node in statements:
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            # A block header that spans lines ends where its body starts.
            if node.lineno <= line < body[0].lineno:
                line = max(line, body[0].lineno - 1)
        elif node.lineno <= line <= (node.end_lineno or node.lineno):
            line = max(line, node.end_lineno or node.lineno)
    return line


def insert_scene_code(source: str, line: int, snippet: str) -> tuple[str, int, int]:
    """Insert *snippet* below *line* as the recorder does, refused unless the
    program still compiles; returns the new source, the first inserted line
    and how many."""
    lines = source.split("\n")
    indent = None
    anchor = lines[line - 1].strip() if 0 < line <= len(lines) else "x"
    if not anchor or anchor.startswith("#"):
        # A blank or comment line has no indentation of its own: the code
        # joins the block the next statement is in.
        following = next(
            (t for t in lines[line:] if t.strip() and not t.lstrip().startswith("#")),
            "",
        )
        indent = following[: len(following) - len(following.lstrip(" \t"))]
    updated, first, count = insert_below_line(
        source, snippet.rstrip("\n"), line, indent
    )
    try:
        compile(updated, "<program>", "exec")
    except SyntaxError as error:
        raise ValueError(
            f"Choose a complete statement in a valid program before inserting: {error.msg}"
        ) from error
    return updated, first, count


@dataclass(frozen=True)
class LiteralTarget:
    call: ast.Call
    vector: ast.List | ast.Tuple
    kind: Literal["joints", "pose"]
    keyword: ast.keyword | None


@lru_cache(maxsize=8)
def _motion_calls(source: str) -> tuple[ast.Call, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(
            f"Correct the Python syntax before editing: {error.msg}"
        ) from error
    return tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("move_j", "move_l", "move_p", "move_c")
    )


def motion_call(source: str, line: int) -> ast.Call:
    calls = [
        node
        for node in _motion_calls(source)
        if node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    if len(calls) != 1:
        raise ValueError("This source location does not identify one motion call.")
    return calls[0]


def target_binding(source: str, line: int, joint_count: int) -> LiteralTarget:
    call = motion_call(source, line)
    assert isinstance(call.func, ast.Attribute)
    if call.func.attr == "move_c":
        raise ValueError(
            "This arc has multiple target poses; edit its arguments in the program."
        )
    keywords = {k.arg: k for k in call.keywords}
    if None in keywords:
        raise ValueError(
            "Expanded keyword arguments can change the motion frame; edit this call in the program."
        )
    for key, allowed in (("rel", False), ("frame", "WRF")):
        if key in keywords:
            try:
                value = ast.literal_eval(keywords[key].value)
            except (ValueError, TypeError, SyntaxError):
                value = None
            if type(value) is not type(allowed) or value != allowed:
                raise ValueError(
                    "Relative or frame-dependent targets must be edited through their binding."
                )
    kind: Literal["joints", "pose"] = "joints" if call.func.attr == "move_j" else "pose"
    name = "angles" if kind == "joints" else "pose"
    if call.func.attr == "move_j" and "pose" in keywords:
        if call.args or "angles" in keywords:
            raise ValueError("This call has more than one possible target argument.")
        kind, name = "pose", "pose"
    keyword = keywords.get(name)
    if len(call.args) > 1 or (call.args and keyword is not None):
        raise ValueError("This call has more than one possible target argument.")
    argument = call.args[0] if call.args else keyword.value if keyword else None
    count = joint_count if kind == "joints" else 6
    if not isinstance(argument, ast.List | ast.Tuple):
        raise ValueError(
            "Computed targets are inspectable; edit their expression in the program."
        )
    if len(argument.elts) != count:
        raise ValueError(f"Direct editing requires {count} literal target coordinates.")
    for node in argument.elts:
        try:
            value = ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError):
            value = None
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(
                "Computed targets are inspectable; edit their expression in the program."
            )
    return LiteralTarget(call, argument, kind, keyword)


def replace_target(
    source: str,
    line: int,
    joint_count: int,
    *,
    pose: list[float],
    joints: list[float] | None = None,
    as_joints: bool = False,
) -> str:
    binding = target_binding(source, line, joint_count)
    values = joints if as_joints or binding.kind == "joints" else pose
    count = joint_count if as_joints or binding.kind == "joints" else 6
    if (
        values is None
        or len(values) != count
        or not all(math.isfinite(v) for v in values)
    ):
        raise ValueError("The target does not have a complete finite pose.")
    if len(binding.vector.elts) != len(values):
        raise ValueError(
            "Converting this target would change its coordinate structure."
        )
    lines = source.encode("utf-8").splitlines(keepends=True)
    offsets = [0]
    for text in lines:
        offsets.append(offsets[-1] + len(text))

    def span(node: ast.AST) -> tuple[int, int]:
        assert isinstance(node, ast.expr | ast.keyword)
        assert node.end_lineno is not None and node.end_col_offset is not None
        return offsets[node.lineno - 1] + node.col_offset, offsets[
            node.end_lineno - 1
        ] + node.end_col_offset

    edits = [
        (*span(node), f"{value:.3f}".encode())
        for node, value in zip(binding.vector.elts, values)
    ]
    if as_joints and binding.kind != "joints":
        if any(
            k.arg
            not in {"pose", "speed", "accel", "duration", "r", "rel", "wait", "timeout"}
            for k in binding.call.keywords
        ):
            raise ValueError(
                "This Cartesian call has arguments that cannot be preserved in a joint move."
            )
        function = binding.call.func
        assert isinstance(function, ast.Attribute)
        _, end = span(function)
        edits.append((end - len(function.attr), end, b"move_j"))
        if binding.keyword is not None:
            start, _ = span(binding.keyword)
            edits.append((start, start + len(binding.keyword.arg or ""), b"angles"))
    result = source.encode("utf-8")
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result.decode("utf-8")
