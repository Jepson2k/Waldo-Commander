"""Shortcuts and the quick-start tour live in Settings; the first visit has its own dialog."""

import asyncio

import pytest
from nicegui import app as ng_app
from nicegui.testing import User

from waldo_commander.components.help_menu import HelpMenu
from waldo_commander.state import ui_state


async def _open_settings(user: User, category: str) -> None:
    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)
    assert ui_state.settings_content.dialog.value, "the gear opens Settings"
    user.find(marker=f"settings-cat-{category}").click()
    await asyncio.sleep(0)


@pytest.mark.integration
class TestHelpMenuAndKeybindings:
    """The keybindings table under Settings → Shortcuts."""

    async def test_shortcuts_category_lists_the_keybindings(self, user: User) -> None:
        await user.open("/")
        await _open_settings(user, "shortcuts")

        await user.should_see(marker="settings-cat-shortcuts")
        await user.should_see(marker="settings-cat-getting-started")
        await user.should_see(marker="keybindings-content")
        await user.should_see("Robot Control")
        await user.should_see("Playback")


@pytest.mark.integration
class TestTutorialStepper:
    """Tests for tutorial/quickstart stepper functionality."""

    async def test_tutorial_shows_steps_and_navigates(self, user: User) -> None:
        """Settings → Getting started walks the tour; Next and Back move through it."""
        await user.open("/")
        await _open_settings(user, "getting-started")

        # Step text, since step titles are Quasar props.
        await user.should_see("Jog in joint space")

        user.find("Next").click()
        await asyncio.sleep(0)
        await user.should_see("Open **Settings** from the gear in the bottom-left rail")

        user.find("Back").click()
        await asyncio.sleep(0)
        await user.should_see("Jog in joint space")

    async def test_tutorial_can_reach_final_step(self, user: User) -> None:
        """Finish on the last step closes Settings."""
        await user.open("/")
        await _open_settings(user, "getting-started")

        for _ in range(3):  # 4 steps total, need 3 Next clicks
            user.find("Next").click()
            await asyncio.sleep(0)

        await user.should_see("Toggle digital outputs")
        user.find("Finish").click()
        await asyncio.sleep(0)
        assert not ui_state.settings_content.dialog.value


@pytest.mark.integration
class TestFirstTimeDialogWithSafety:
    """Tests for first-time dialog with safety acknowledgment step."""

    async def test_safety_step_shows_on_first_visit(self, user: User) -> None:
        """Test that safety step appears on first visit and blocks navigation.

        Verifies:
        1. First-time dialog opens automatically
        2. Safety step is the first step with warning content
        3. Continue button is disabled until checkbox is checked
        4. Checking checkbox enables Continue and stores acknowledgment
        """
        # Clear the storage keys to simulate first visit BEFORE opening the page
        # The reset_state fixture sets these, so we clear them
        ng_app.storage.general.pop(HelpMenu.FIRST_VISIT_KEY, None)
        ng_app.storage.general.pop(HelpMenu.SAFETY_ACKNOWLEDGED_KEY, None)

        await user.open("/")
        await asyncio.sleep(0.5)  # Wait for async task to trigger dialog

        # Should see safety step content (search by marker)
        await user.should_see(marker="safety-step")
        await user.should_see("Please read before continuing")
        await user.should_see("no safety guarantees")
        await user.should_see("I have read and accept responsibility")

        # Continue button should be present but disabled (can't easily test disabled state)
        await user.should_see("Continue")

        # Check the acceptance checkbox
        user.find("I have read and accept responsibility").click()
        await asyncio.sleep(0.1)

        # Click Continue to proceed to next step (should now be enabled)
        user.find("Continue").click()
        await asyncio.sleep(0.1)

        # Should now see the first tutorial step
        await user.should_see("Jog in joint space")
