"""Pre-commit check: colour values live in theme.py only.

Everything else refers to a token: ``var(--wc-<name>)`` in CSS and styles,
``wc-<name>`` in Quasar props, ``text-wc-<name>`` / ``bg-wc-<name>`` classes,
or ``hex_of("<name>")`` / ``linear_rgb("<name>")`` in Python. A filled ``wc-*``
colour in a props string always names its text colour too, because Quasar's
own white-on-fill default lives in a CSS layer that outranks app rules.
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "waldo_commander"
SUFFIXES = {".py", ".js", ".svg", ".css", ".vue"}
ALLOWED = {PACKAGE / "common" / "theme.py"}
# Pictograms load as <img>, where the page's tokens can't reach; vendored
# third-party code stays as released.
ALLOWED_DIRS = {
    PACKAGE / "static" / "icons" / "skills",
    PACKAGE / "scene3d" / "vendor",
}

QUASAR_HUES = (
    "red|pink|purple|deep-purple|indigo|blue|light-blue|cyan|teal|green|light-green|"
    "lime|yellow|amber|orange|deep-orange|brown|grey|gray|blue-grey"
)
QUASAR_SEMANTIC = "primary|secondary|accent|positive|negative|warning|info|dark"
TAILWIND_HUES = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|"
    "cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
)
NAMED_COLOURS = (
    "white|black|red|green|blue|yellow|orange|purple|pink|brown|gray|grey|cyan|"
    "magenta|lime|teal|navy|maroon|olive|silver|aqua|fuchsia|gold|indigo|violet|"
    "coral|salmon|crimson|tomato|khaki|beige|ivory|lavender|turquoise|tan|plum|"
    "orchid|chocolate|whitesmoke|gainsboro|lightgray|lightgrey|darkgray|darkgrey|"
    "dimgray|dimgrey|darkred|darkgreen|darkblue|lightblue|skyblue|steelblue"
)
COLOR_PROPS = (
    "color|text-color|thumb-color|track-color|label-color|label-text-color|"
    "selected-color|active-color|done-color|toggle-color|icon-color|bg-color"
)
CSS_COLOR_PROPERTIES = (
    "color|background(?:-color)?|fill|stroke|stop-color|border(?:-[a-z]+)*|"
    "outline(?:-color)?|box-shadow|text-shadow|caret-color"
)
FILLS = (
    "control|action|estop|record|run|mode-sim|warning-fill|"
    "fill-positive|fill-warning|fill-error|fill-info"
)

RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "hex colour literal",
        re.compile(
            r"""["'(:,=\s]#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])"""
        ),
    ),
    ("rgb()/rgba() literal", re.compile(r"(?<![\w-])rgba?\(")),
    ("Tailwind palette variable", re.compile(r"var\(--color-")),
    (
        "legacy --ctk/--sem/--glass variable",
        re.compile(r"var\(--(ctk|sem|glass|overlay|axis|on|sim|brand)-"),
    ),
    (
        "Quasar palette name in a colour prop",
        re.compile(rf"""(?:{COLOR_PROPS})\s*=\s*["']?(?:{QUASAR_HUES})(?:-\d+)?\b"""),
    ),
    (
        "Quasar semantic colour outside ui.notify (use a wc-* token)",
        re.compile(
            rf"""(?:{COLOR_PROPS})\s*=\s*["']?(?:{QUASAR_SEMANTIC})\b"""
            rf"""|(?<![\w-])(?:text|bg)-(?:{QUASAR_SEMANTIC})(?![\w-])"""
        ),
    ),
    (
        "named colour",
        re.compile(
            rf"""(?:{COLOR_PROPS})\s*=\s*["']?(?:{NAMED_COLOURS})(?![\w-])"""
            rf"""|(?<![\w-])(?:{CSS_COLOR_PROPERTIES})\s*:[^;"'{{}}]*?(?<![\w-])(?:{NAMED_COLOURS})(?![\w-])"""
        ),
    ),
    (
        "palette colour class",
        re.compile(
            rf"""(?<![\w-])(?:text|bg|border|fill|stroke)-(?:{QUASAR_HUES}|{TAILWIND_HUES})(?:-\d+)?(?![\w-])"""
            r"""|(?<![\w-])(?:text|bg)-(?:white|black)(?![\w-])"""
        ),
    ),
    (
        'colour keyword argument (use .props("color=wc-..."), which needs no registration order)',
        re.compile(r"""\bcolor=["']wc-"""),
    ),
]

# An interpolated colour counts as a fill: which one it is only shows at runtime.
FILL = re.compile(rf"(?<![\w-])color=(?:wc-(?:{FILLS})(?![\w-])|\{{)")
TEXT_COLOR = re.compile(r"(?<![\w-])text-color=")
UNFILLED = re.compile(r"(?<![\w-])(?:flat|outline)(?![\w-])")
FSTRING_START = getattr(tokenize, "FSTRING_START", -1)
FSTRING_END = getattr(tokenize, "FSTRING_END", -1)

Pos = tuple[int, int]


def _python_view(text: str) -> tuple[list[str], list[tuple[int, str]]]:
    """The lines with comments and ``ui.notify`` calls blanked out, and each
    string literal outside those calls (adjacent literals joined, f-strings
    whole) with the line it starts on."""
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    masked = list(text)

    def blank(start: Pos, end: Pos) -> None:
        for i in range(offsets[start[0] - 1] + start[1], offsets[end[0] - 1] + end[1]):
            if masked[i] != "\n":
                masked[i] = " "

    notify_spans: list[tuple[Pos, Pos]] = []
    for i, tok in enumerate(tokens):
        if tok.type == tokenize.COMMENT:
            blank(tok.start, tok.end)
        call = [t.string for t in tokens[i : i + 4]]
        if (
            call[:2] == ["ui", "."]
            and call[2:3] in (["notify"], ["notification"])
            and call[3:] == ["("]
        ):
            depth = 0
            for close in tokens[i + 3 :]:
                if close.type == tokenize.OP and close.string in "([{":
                    depth += 1
                elif close.type == tokenize.OP and close.string in ")]}":
                    depth -= 1
                    if depth == 0:
                        notify_spans.append((tok.start, close.end))
                        blank(tok.start, close.end)
                        break

    literals: list[tuple[Pos, Pos]] = []
    current: list[Pos] | None = None
    depth = 0
    for tok in tokens:
        if tok.type == FSTRING_START:
            if depth == 0 and current is None:
                current = [tok.start, tok.end]
            depth += 1
        elif depth:
            if tok.type == FSTRING_END:
                depth -= 1
                if depth == 0 and current is not None:
                    current[1] = tok.end
        elif tok.type == tokenize.STRING:
            if current is None:
                current = [tok.start, tok.end]
            else:
                current[1] = tok.end
        elif tok.type not in (tokenize.NL, tokenize.COMMENT) and current is not None:
            literals.append((current[0], current[1]))
            current = None

    def source(start: Pos, end: Pos) -> str:
        return text[offsets[start[0] - 1] + start[1] : offsets[end[0] - 1] + end[1]]

    strings = [
        (start[0], source(start, end))
        for start, end in literals
        if not any(lo <= start < hi for lo, hi in notify_spans)
    ]
    return "".join(masked).splitlines(), strings


def check_text(text: str, suffix: str = ".py") -> list[tuple[int, str]]:
    """``(line, rule)`` for each colour in a file's text that bypasses the tokens."""
    if suffix == ".py":
        lines, strings = _python_view(text)
    else:
        lines, strings = text.splitlines(), []
    problems = [
        (lineno, label)
        for lineno, line in enumerate(lines, 1)
        for label, rx in RULES
        if rx.search(line)
    ]
    problems += [
        (lineno, "filled wc-* colour without text-color")
        for lineno, literal in strings
        if FILL.search(literal)
        and not TEXT_COLOR.search(literal)
        and not UNFILLED.search(literal)
    ]
    return sorted(problems)


def check(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    source = text.splitlines()
    return [
        f"{path.relative_to(ROOT)}:{lineno}: {label}: {source[lineno - 1].strip()[:110]}"
        for lineno, label in check_text(text, path.suffix)
    ]


def _scanned(path: Path) -> bool:
    return (
        path.suffix in SUFFIXES
        and PACKAGE in path.parents
        and path not in ALLOWED
        and not ALLOWED_DIRS.intersection(path.parents)
    )


def main(argv: list[str]) -> int:
    files = (
        [Path(a).resolve() for a in argv]
        if argv
        else sorted(p for p in PACKAGE.rglob("*") if p.is_file())
    )
    problems = [p for f in files if _scanned(f) for p in check(f)]
    for p in problems:
        print(p)
    if problems:
        print(
            f"\n{len(problems)} colour problem(s) outside theme.py; refer to a token instead."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
