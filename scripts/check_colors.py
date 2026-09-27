"""Pre-commit check: colour values live in theme.py only.

Everything else refers to a token: ``var(--wc-<name>)`` in CSS and styles,
``wc-<name>`` in Quasar props, ``text-wc-<name>`` / ``bg-wc-<name>`` classes,
or ``hex_of("<name>")`` / ``rgb01("<name>")`` in Python. A filled ``wc-*``
colour in a props string always names its text colour too, because Quasar's
own white-on-fill default lives in a CSS layer that outranks app rules.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "waldo_commander"
ALLOWED = {PACKAGE / "common" / "theme.py"}

QUASAR_HUES = (
    "red|pink|purple|deep-purple|indigo|blue|light-blue|cyan|teal|green|light-green|"
    "lime|yellow|amber|orange|deep-orange|brown|grey|gray|blue-grey"
)
QUASAR_SEMANTIC = "primary|secondary|accent|positive|negative|warning|info|dark"
TAILWIND_HUES = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|"
    "cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
)
COLOR_PROPS = (
    "color|text-color|thumb-color|track-color|label-color|label-text-color|"
    "selected-color|active-color|done-color|toggle-color|icon-color|bg-color"
)
FILLS = "control|action|estop|run|mode-sim|warning-fill|fill-positive|fill-warning|fill-error"

RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "hex colour literal",
        re.compile(
            r"""["'(:,\s]#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}(?:[0-9a-fA-F]{2})?)?\b"""
        ),
    ),
    ("rgb()/rgba() literal", re.compile(r"\brgba?\(")),
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
            rf"""(?:{COLOR_PROPS})\s*=\s*["']?(?:{QUASAR_SEMANTIC})\b|\b(?:text|bg)-(?:{QUASAR_SEMANTIC})\b"""
        ),
    ),
    (
        "literal white in a colour prop",
        re.compile(rf"""(?:{COLOR_PROPS})\s*=\s*["']?white\b"""),
    ),
    (
        "Tailwind colour class",
        re.compile(
            rf"""\b(?:text|bg|border|fill|stroke)-(?:{TAILWIND_HUES})-\d{{2,3}}\b|\b(?:text|bg)-(?:white|black)\b"""
        ),
    ),
    (
        'colour keyword argument (use .props("color=wc-..."), which needs no registration order)',
        re.compile(r"""\bcolor=["']wc-"""),
    ),
]

STRING = re.compile(r"""(f?)(["'])((?:(?!\2).)*)\2""")
FILL_IN_STRING = re.compile(rf"\bcolor=wc-(?:{FILLS})\b")
NOTIFY_START = re.compile(r"ui\.(?:notify|notification)\(")


def _unpaired_fills(line: str) -> bool:
    """A filled wc-* colour in a props string without its text-color."""
    for m in STRING.finditer(line):
        body = m.group(3)
        if not FILL_IN_STRING.search(body) or "text-color=" in body:
            continue
        if "flat" in body or "outline" in body or "track-color" in body:
            continue
        return True
    return False


def check(path: Path) -> list[str]:
    problems = []
    text = path.read_text(encoding="utf-8")
    depth = 0  # parentheses still open from a ui.notify(...) call
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if depth == 0 and NOTIFY_START.search(line):
            depth = line.count("(") - line.count(")")
            continue
        if depth > 0:
            depth += line.count("(") - line.count(")")
            continue
        for label, rx in RULES:
            if rx.search(line):
                problems.append(
                    f"{path.relative_to(ROOT)}:{lineno}: {label}: {stripped[:110]}"
                )
        if _unpaired_fills(line):
            problems.append(
                f"{path.relative_to(ROOT)}:{lineno}: filled wc-* colour without text-color: {stripped[:110]}"
            )
    return problems


def main(argv: list[str]) -> int:
    files = [Path(a).resolve() for a in argv] if argv else sorted(PACKAGE.rglob("*.py"))
    problems: list[str] = []
    for f in files:
        if f in ALLOWED or f.suffix != ".py" or PACKAGE not in f.parents:
            continue
        problems.extend(check(f))
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
