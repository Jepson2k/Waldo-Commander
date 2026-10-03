"""Waldo, the little robot that fronts Waldo Commander's state.

One element per appearance (status chip, e-stop dialog, loading screen, ...).
Each instance animates on its own in the browser — blinking, glancing
around, dozing off, following the pointer — while the server only sets its
``mood``, whether it is ``busy``, which antenna ``light`` is on, and asks for
one-shot reactions.
"""

from enum import StrEnum

from nicegui import app, ui

CALM_STORAGE_KEY = "ui/calm_buddy"


def calm_preferred() -> bool:
    """The user's "Calm robot" setting: no idle fidgets anywhere Waldo appears."""
    return bool(app.storage.general.get(CALM_STORAGE_KEY, False))


class Mood(StrEnum):
    """Resting expression and colour."""

    HAPPY = "happy"
    """Green: connected to the robot."""
    NEUTRAL = "neutral"
    """Amber: driving the simulator."""
    SAD = "sad"
    """Red: robot mode with no robot answering."""
    ALARMED = "alarmed"
    """Red, wide-eyed, flashing antennae: an E-STOP is active."""
    BOOTING = "booting"
    """Muted grey with a scanning visor: waiting for the controller."""


class Light(StrEnum):
    """A steady antenna light for a standing condition."""

    RECORDING = "rec"
    """Red dot on one bulb: the motion recorder is capturing."""
    AGENT = "agent"
    """Both bulbs pulse: an AI agent holds control of the arm."""


class Reaction(StrEnum):
    """One-shot animations, after which the buddy settles back into its mood."""

    GREET = "greet"
    CELEBRATE = "celebrate"
    OOPS = "oops"
    STARTLE = "startle"
    SHRUG = "shrug"
    NOD = "nod"


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
        :param color: CSS colour overriding the mood's colour, such as a
            ``var(--wc-...)`` token or ``currentColor`` to take a chip's text colour
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
        self._props["light"] = ""
        self._props["calm"] = calm_preferred()
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
    def light(self) -> Light | None:
        return Light(self._props["light"]) if self._props["light"] else None

    @property
    def calm(self) -> bool:
        return self._props["calm"]

    @property
    def sleep_after_s(self) -> float:
        return self._props["sleepAfter"]

    @property
    def last_reaction(self) -> Reaction | None:
        reaction = self._props["reaction"]
        return Reaction(reaction["name"]) if reaction else None

    def _set(self, prop: str, value: object) -> None:
        if self._props[prop] != value:
            self._props[prop] = value
            self.update()

    def set_mood(self, mood: Mood) -> None:
        self._set("mood", mood.value)

    def set_busy(self, busy: bool) -> None:
        """Busy buddies focus: lids lower, eyes on the arm, antenna LEDs chase."""
        self._set("busy", busy)

    def set_light(self, light: Light | None) -> None:
        self._set("light", light.value if light else "")

    def set_calm(self, calm: bool) -> None:
        """Calm buddies skip idle fidgets, breathing, pointer-following and
        sleep; reactions to what the robot does still play."""
        self._set("calm", calm)

    def set_sleep_after(self, seconds: float) -> None:
        """Doze off after this long idle; 0 keeps it awake (and wakes it)."""
        self._set("sleepAfter", seconds)

    def react(self, reaction: Reaction) -> None:
        self._reaction_seq += 1
        self._props["reaction"] = {"name": reaction.value, "seq": self._reaction_seq}
        self.update()
