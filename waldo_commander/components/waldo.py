"""Waldo, the little robot that fronts Waldo Commander's state.

One element per appearance (status chip, e-stop dialog, loading screen, ...).
Each instance animates on its own in the browser — blinking, glancing
around, dozing off, following the pointer — while the server only sets its
``mood``, whether it is ``busy``, which antenna ``light`` is on, an AI
``agent``'s part in the session, where a jog holds its ``look``, and asks
for one-shot reactions. Its eyes, mouth and LEDs are cut out of its body,
so whatever it sits on shows through them.
"""

from enum import StrEnum

from nicegui import app, ui

CALM_STORAGE_KEY = "ui/calm_waldo"


def calm_preferred() -> bool:
    """The user's "Calm Waldo" setting: no idle fidgets anywhere Waldo appears."""
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
    """Red bulb: the motion recorder is capturing."""


class Agent(StrEnum):
    """An AI agent's part in the session, drawn in its control mode's colour
    (``--waldo-ai``) on the antenna tips."""

    PRESENT = "present"
    """An MCP client is connected: the tips light up."""
    DRIVING = "driving"
    """It holds control of the arm: the tips pulse and the eyes take its colour."""


class Reaction(StrEnum):
    """One-shot animations, after which Waldo settles back into its mood."""

    GREET = "greet"
    CELEBRATE = "celebrate"
    OOPS = "oops"
    STARTLE = "startle"
    SHRUG = "shrug"
    NOD = "nod"
    HEADSHAKE = "headshake"
    WARNING = "warning"
    """Wide eyes darting about and a bead of sweat."""
    ERROR = "error"
    """Squeezed-shut eyes, a gritted mouth and a shake."""
    START = "start"
    """A program starts: Waldo focuses and settles in."""
    HOME = "home"
    """The eyes roll once around, then a nod."""
    GRIP_OPEN = "grip-open"
    GRIP_CLOSE = "grip-close"
    TOOL = "tool"
    """A spin and a sparkle for a new tool."""
    AI_MODE = "ai-mode"
    """The antenna tips flash in a new AI control mode's colour."""


class Waldo(ui.element, component="waldo.vue"):
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
        self._props["agent"] = ""
        self._props["asking"] = False
        self._props["look"] = None
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
    def agent(self) -> Agent | None:
        return Agent(self._props["agent"]) if self._props["agent"] else None

    @property
    def asking(self) -> bool:
        return self._props["asking"]

    @property
    def look(self) -> tuple[float, float, float] | None:
        look = self._props["look"]
        return (look[0], look[1], look[2]) if look else None

    @property
    def last_reaction(self) -> Reaction | None:
        reaction = self._props["reaction"]
        return Reaction(reaction["name"]) if reaction else None

    @property
    def peeked(self) -> bool:
        """Whether the last reaction was played as a peek."""
        reaction = self._props["reaction"]
        return bool(reaction and reaction.get("peek"))

    def _set(self, prop: str, value: object) -> None:
        if self._props[prop] != value:
            self._props[prop] = value
            self.update()

    def set_mood(self, mood: Mood) -> None:
        self._set("mood", mood.value)

    def set_busy(self, busy: bool) -> None:
        """A busy Waldo focuses: lids lower, eyes on the arm, antenna LEDs chase."""
        self._set("busy", busy)

    def set_light(self, light: Light | None) -> None:
        self._set("light", light.value if light else "")

    def set_calm(self, calm: bool) -> None:
        """A calm Waldo skips idle fidgets, breathing, pointer-following and
        sleep; reactions to what the robot does still play."""
        self._set("calm", calm)

    def set_sleep_after(self, seconds: float) -> None:
        """Doze off after this long idle; 0 keeps it awake (and wakes it)."""
        self._set("sleepAfter", seconds)

    def set_agent(self, agent: Agent | None) -> None:
        """Arriving, leaving, taking control and handing it back each play
        their own reaction."""
        self._set("agent", agent.value if agent else "")

    def set_asking(self, asking: bool) -> None:
        """A question mark in the AI's colour while a request waits for the
        human; it tilts its head and looks up at it as one arrives."""
        self._set("asking", asking)

    def set_look(self, look: tuple[float, float, float] | None) -> None:
        """Hold the eyes along ``(dx, dy)`` in -1..1 and tilt the head by the
        third value in degrees, until ``None`` lets go; a hold shorter than a
        glance is kept long enough to read."""
        self._set("look", list(look) if look else None)

    def react(self, reaction: Reaction) -> None:
        self._send(reaction, peek=False)

    def peek(self, reaction: Reaction) -> None:
        """Rise into view from below the clipping element this Waldo rests
        under, play *reaction*, and sink back out of sight."""
        self._send(reaction, peek=True)

    def _send(self, reaction: Reaction, *, peek: bool) -> None:
        self._reaction_seq += 1
        self._props["reaction"] = {
            "name": reaction.value,
            "seq": self._reaction_seq,
            "peek": peek,
        }
        self.update()
