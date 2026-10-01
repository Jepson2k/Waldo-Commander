"""Helpers for screen tests that share one browser session across a class."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from selenium.webdriver.support.ui import WebDriverWait

from tests.conftest import TEST_WINDOW_HEIGHT, TEST_WINDOW_WIDTH

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


def wait(screen: "Screen", timeout: float = 10.0) -> WebDriverWait:
    """A ``WebDriverWait`` that polls every 50 ms rather than Selenium's 500 ms."""
    return WebDriverWait(screen.selenium, timeout, poll_frequency=0.05)


@contextmanager
def window_size(screen: "Screen", width: int, height: int) -> Iterator[None]:
    """Resize the browser window, and give the next test in the class the
    standard window back."""
    screen.selenium.set_window_size(width, height)
    try:
        yield
    finally:
        screen.selenium.set_window_size(TEST_WINDOW_WIDTH, TEST_WINDOW_HEIGHT)


def no_visible(screen: "Screen", selector: str) -> bool:
    """Whether nothing matching ``selector`` is on screen, checked in JS: an
    empty Selenium lookup blocks for the driver's implicit wait."""
    return screen.selenium.execute_script(
        "return ![...document.querySelectorAll(arguments[0])]"
        ".some(e => e.getClientRects().length > 0);",
        selector,
    )
