"""The committed token module is exactly what the spec generates, and the
adapters refuse tokens that have no single hex."""

import importlib.util
import json
from pathlib import Path

import pytest

from waldo_commander.common import theme, tokens

ROOT = Path(__file__).resolve().parent.parent


def _generator():
    spec = importlib.util.spec_from_file_location(
        "gen_tokens", ROOT / "scripts" / "gen_tokens.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tokens_module_matches_spec() -> None:
    gen = _generator()
    rendered = gen.render(json.loads((ROOT / "design" / "tokens.json").read_text()))
    assert (
        ROOT / "waldo_commander" / "common" / "tokens.py"
    ).read_text() == rendered, "tokens.py is stale: run scripts/gen_tokens.py"


def test_aliases_resolve_and_hex_matches_css() -> None:
    gen = _generator()
    for name, per_theme in tokens.COLOR.items():
        for theme_key, value in per_theme.items():
            resolved = value
            while resolved.startswith("{"):
                resolved = tokens.COLOR[resolved[1:-1]][theme_key]
            rgba = gen.parse(resolved)
            if rgba[3] < 1.0:
                assert name not in tokens.COLOR_HEX
            else:
                assert tokens.COLOR_HEX[name][theme_key] == gen.to_hex(rgba)


def test_translucent_tokens_have_no_hex() -> None:
    with pytest.raises(KeyError):
        theme.hex_of("positive-soft")
    with pytest.raises(KeyError):
        theme.rgb01("glass", "dark")
    assert theme.hex_of("estop", "dark").startswith("#")
