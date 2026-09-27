"""Robot buddy: the animated little robot that fronts Waldo Commander's state.

One element per appearance (status chip, e-stop dialog, loading screen, ...).
Each instance animates on its own in the browser — blinking, glancing
around, dozing off, following the pointer — while the server only sets its
``mood``, whether it is ``busy``, and asks for one-shot reactions.
"""

from enum import StrEnum

from nicegui import ui


class Mood(StrEnum):
    """Resting expression and colour."""

    HAPPY = "happy"
    """Green: connected to the robot."""
    NEUTRAL = "neutral"
    """Grey: driving the simulator."""
    SAD = "sad"
    """Red: robot mode with no robot answering."""
    ALARMED = "alarmed"
    """Red, wide-eyed, flashing antennae: an E-STOP is active."""
    BOOTING = "booting"
    """Grey with a scanning visor: waiting for the controller."""


class Reaction(StrEnum):
    """One-shot animations, after which the buddy settles back into its mood."""

    GREET = "greet"
    CELEBRATE = "celebrate"
    OOPS = "oops"
    STARTLE = "startle"


class RobotBuddy(ui.element, component="robot_buddy.vue"):
    def __init__(
        self,
        mood: Mood = Mood.HAPPY,
        *,
        size: int = 36,
        color: str | None = None,
        interactive: bool = False,
        sleep_after_s: float = 0.0,
        roam_avoid: str | None = None,
    ) -> None:
        """Animated robot character.

        :param mood: resting expression and colour
        :param size: edge length of the square element in pixels
        :param color: CSS colour overriding the mood's colour
        :param interactive: follow the pointer when it comes near, and giggle
            (or get dizzy) when clicked
        :param sleep_after_s: doze off after this long without pointer or
            keyboard activity; 0 never sleeps
        :param roam_avoid: wander the viewport DVD-screensaver style, bouncing
            off its edges and off the element this CSS selector matches
        """
        super().__init__()
        self._props["mood"] = Mood(mood).value
        self._props["color"] = color
        self._props["busy"] = False
        self._props["interactive"] = interactive
        self._props["sleepAfter"] = sleep_after_s
        self._props["roam"] = roam_avoid is not None
        self._props["roamAvoid"] = roam_avoid or ""
        self._props["reaction"] = None
        self._reaction_seq = 0
        self.style(f"width: {size}px; height: {size}px")

    @property
    def mood(self) -> Mood:
        return Mood(self._props["mood"])

    @property
    def busy(self) -> bool:
        return self._props["busy"]

    @property
    def last_reaction(self) -> Reaction | None:
        reaction = self._props["reaction"]
        return Reaction(reaction["name"]) if reaction else None

    def set_mood(self, mood: Mood) -> None:
        if self._props["mood"] != mood.value:
            self._props["mood"] = mood.value
            self.update()

    def set_busy(self, busy: bool) -> None:
        """Busy buddies focus: lids lower, eyes on the arm, antenna LEDs chase."""
        if self._props["busy"] != busy:
            self._props["busy"] = busy
            self.update()

    def react(self, reaction: Reaction) -> None:
        self._reaction_seq += 1
        self._props["reaction"] = {"name": reaction.value, "seq": self._reaction_seq}
        self.update()
