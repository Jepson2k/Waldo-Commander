"""The pre-commit colour check finds colours that bypass the design tokens."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_colors", Path(__file__).parent.parent / "scripts" / "check_colors.py"
)
assert _spec is not None and _spec.loader is not None
check_colors = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_colors)


def flagged(text: str, suffix: str = ".py") -> list[int]:
    return sorted({line for line, _ in check_colors.check_text(text, suffix)})


_CASES = [
    # A notify call is skipped by its brackets, not the parens in its strings.
    (
        'ui.notify("Saved (2 files", color="positive")\n'
        'btn.props("color=red")\n'
        "ui.notify(\n"
        '    "done :)",\n'
        '    color="warning",\n'
        ")\n",
        ".py",
        [2],
    ),
    # Fill pairing reads whole prop names.
    (
        'slider.props("color=wc-progress thumb-color=wc-control")\n'
        'icon.props("color=wc-action-text")\n'
        'btn.props("round color=wc-action")\n'
        'btn.props("round color=wc-action"\n'
        '          " text-color=wc-on-bright")\n'
        'btn.props("flat color=wc-control")\n',
        ".py",
        [3],
    ),
    # An interpolated fill needs its text colour.
    (
        'btn.props(f"color={fill}")\n'
        "btn.props(f\"color={'wc-action' if on else 'wc-control'}\")\n"
        'btn.props(f"color={fill} text-color={text}")\n'
        'btn.props(f"size={size} color=wc-control text-color=wc-text")\n',
        ".py",
        [1, 2],
    ),
    # Palette classes are caught with or without a shade.
    (
        'ui.label().classes("text-grey")\n'
        'ui.label().classes("bg-teal-7")\n'
        'ui.label().classes("text-sky-300")\n'
        'ui.label().classes("text-wc-text-muted bg-wc-surface text-center")\n',
        ".py",
        [1, 2, 3],
    ),
    # Every hex length is caught; an element id selector is not a colour.
    (
        'el.style("color: #fff8")\n'
        'el.style("color: #ffffff80")\n'
        'el.style("color: #abc")\n'
        "tooltip.props(f'target=\"#c{el.id}\"')\n",
        ".py",
        [1, 2, 3],
    ),
    # Named colours are caught in styles and props, not in comments.
    (
        'el.style("background: black")\n'
        'el.style("border: 1px solid white")\n'
        'btn.props("text-color=white")\n'
        'el.style("color: var(--wc-text); background: transparent")\n'
        "# a comment may say white\n",
        ".py",
        [1, 2, 3],
    ),
    # Scripts, stylesheets and SVGs are checked too.
    ('<circle fill="var(--face-cut, #fff)"/>\n', ".svg", [1]),
    ('<rect fill="currentColor"/>\n', ".svg", []),
    ("ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';\n", ".js", [1]),
    (".x { color: red; }\n.y { color: var(--wc-text); }\n", ".css", [1]),
]


def test_the_check_flags_exactly_the_lines_that_bypass_the_tokens():
    for text, suffix, expected in _CASES:
        assert flagged(text, suffix) == expected, (suffix, text)
