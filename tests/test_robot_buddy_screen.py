"""Browser-level check that the robot buddy actually animates.

Everything the buddy does between the server's mood/reaction updates —
greeting, following the pointer, answering pokes, raising the alarm —
happens in its Vue component, so only a real browser can see it. Each
expression is a layer whose opacity the component drives; the checks
read those layers off the live DOM.
"""

import re
import time

import pytest
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import dismiss_dialogs, js

CHIP = ".q-chip .robot-buddy"
DIALOG = ".q-dialog .robot-buddy"


def _layer_shown(screen, root: str, layer: str) -> bool:
    return bool(
        js(
            screen,
            "const el = document.querySelector(arguments[0] + ' ' + arguments[1]);"
            "return !!el && getComputedStyle(el).opacity === '1';",
            root,
            layer,
        )
    )


def _wait(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _pupil_offset(screen) -> tuple[float, float]:
    transform = js(
        screen,
        "return document.querySelector(arguments[0] + ' .bb-pupil').style.transform;",
        CHIP,
    )
    x, y = (float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", transform)[:2])
    return x, y


@pytest.mark.browser
def test_chip_buddy_greets_watches_the_pointer_and_answers_pokes(screen) -> None:
    screen.open("/")

    # The page waves hello once it has loaded: a wink from the chip.
    _wait(
        lambda: _layer_shown(screen, CHIP, ".bb-eyes-wink"),
        timeout=30.0,
        what="the chip buddy's greeting wink",
    )
    dismiss_dialogs(screen)
    _wait(
        lambda: _layer_shown(screen, CHIP, ".bb-eyes-open")
        and not _layer_shown(screen, CHIP, ".bb-eyes-wink"),
        timeout=5.0,
        what="the greeting to finish",
    )
    assert js(
        screen,
        "return !!document.querySelector(arguments[0] + '.bb-mood-neutral');",
        CHIP,
    ), "the simulator's buddy is the grey one"

    # Pointer down and to the left of the chip: the pupils follow it.
    chip = screen.selenium.find_element(By.CSS_SELECTOR, CHIP)
    ActionChains(screen.selenium).move_to_element_with_offset(chip, -200, 150).perform()
    _wait(
        lambda: _pupil_offset(screen)[0] < -0.3 and _pupil_offset(screen)[1] > 0.2,
        timeout=3.0,
        what="the pupils to look down-left at the pointer",
    )

    # One poke giggles; a flurry makes it dizzy.
    ActionChains(screen.selenium).move_to_element(chip).click().perform()
    _wait(
        lambda: _layer_shown(screen, CHIP, ".bb-eyes-happy"),
        timeout=2.0,
        what="a giggle after one poke",
    )
    # Three more make four inside the poke window.
    ActionChains(screen.selenium).click(chip).click().click().perform()
    _wait(
        lambda: _layer_shown(screen, CHIP, ".bb-eyes-spiral"),
        timeout=2.0,
        what="spiral eyes after a flurry of pokes",
    )

    # E-STOP: the chip sounds the alarm and the dialog brings a big one.
    ActionChains(screen.selenium).move_by_offset(-400, 300).perform()
    ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
    _wait(
        lambda: js(
            screen,
            "return !!document.querySelector(arguments[0] + '.bb-mood-alarmed.bb-leds-alarm')"
            " && !!document.querySelector(arguments[1] + '.bb-leds-alarm');",
            CHIP,
            DIALOG,
        ),
        timeout=5.0,
        what="alarmed buddies in the chip and the E-STOP dialog",
    )
    js(
        screen,
        "for (const b of document.querySelectorAll('.q-dialog button'))"
        " if (/reset/i.test(b.textContent)) { b.click(); break; }",
    )
    _wait(
        lambda: js(
            screen,
            "return !!document.querySelector(arguments[0] + '.bb-mood-neutral');",
            CHIP,
        ),
        timeout=5.0,
        what="the chip buddy to calm down after Reset",
    )
