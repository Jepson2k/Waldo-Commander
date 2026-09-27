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


def test_a_notify_call_is_skipped_by_its_brackets_not_the_parens_in_its_strings():
    text = (
        'ui.notify("Saved (2 files", color="positive")\n'
        'btn.props("color=red")\n'
        "ui.notify(\n"
        '    "done :)",\n'
        '    color="warning",\n'
        ")\n"
    )
    assert flagged(text) == [2]


def test_fill_pairing_reads_whole_prop_names():
    text = (
        'slider.props("color=wc-progress thumb-color=wc-control")\n'
        'icon.props("color=wc-action-text")\n'
        'btn.props("round color=wc-action")\n'
        'btn.props("round color=wc-action"\n'
        '          " text-color=wc-on-bright")\n'
        'btn.props("flat color=wc-control")\n'
    )
    assert flagged(text) == [3]


def test_an_interpolated_fill_needs_its_text_colour():
    text = (
        'btn.props(f"color={fill}")\n'
        "btn.props(f\"color={'wc-action' if on else 'wc-control'}\")\n"
        'btn.props(f"color={fill} text-color={text}")\n'
        'btn.props(f"size={size} color=wc-control text-color=wc-text")\n'
    )
    assert flagged(text) == [1, 2]


def test_palette_classes_are_caught_with_or_without_a_shade():
    text = (
        'ui.label().classes("text-grey")\n'
        'ui.label().classes("bg-teal-7")\n'
        'ui.label().classes("text-sky-300")\n'
        'ui.label().classes("text-wc-text-muted bg-wc-surface text-center")\n'
    )
    assert flagged(text) == [1, 2, 3]


def test_every_hex_length_is_caught():
    text = (
        'el.style("color: #fff8")\n'
        'el.style("color: #ffffff80")\n'
        'el.style("color: #abc")\n'
        "tooltip.props(f'target=\"#c{el.id}\"')\n"
    )
    assert flagged(text) == [1, 2, 3]


def test_named_colours_are_caught_in_styles_and_props():
    text = (
        'el.style("background: black")\n'
        'el.style("border: 1px solid white")\n'
        'btn.props("text-color=white")\n'
        'el.style("color: var(--wc-text); background: transparent")\n'
        "# a comment may say white\n"
    )
    assert flagged(text) == [1, 2, 3]


def test_scripts_stylesheets_and_svgs_are_checked():
    assert flagged('<circle fill="var(--face-cut, #fff)"/>\n', ".svg") == [1]
    assert flagged('<rect fill="currentColor"/>\n', ".svg") == []
    assert flagged("ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';\n", ".js") == [1]
    assert flagged(".x { color: red; }\n.y { color: var(--wc-text); }\n", ".css") == [1]
