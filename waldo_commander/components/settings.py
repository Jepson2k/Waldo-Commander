"""Settings component for serial port, theme, and visualization preferences."""

import asyncio
import logging
import math
import weakref
from collections.abc import Callable
from contextlib import contextmanager
from typing import cast

import waldoctl
from nicegui import Client, background_tasks, context, ui
from nicegui import app as ng_app
from nicegui.client import ClientConnectionTimeout
from waldoctl import EnvelopeMode, Panel, RobotClient, iter_plugin_panels
from waldoctl.setup import PoseValues, TcpCalibration

from waldo_commander.components.simulation_engine import simulation
from waldo_commander.constants import RESERVED_TAB_IDS
from waldo_commander.services.camera_service import (
    camera_service,
    enumerate_video_devices,
)
from waldo_commander.services.motion_recorder import JOG_BLEND_R_MAX, jog_blend_r
from waldo_commander.state import automation_state, simulation_state, ui_state

logger = logging.getLogger(__name__)

# Trailing-edge window for a TCP offset edit, seconds.
TCP_EDIT_THROTTLE_S = 0.4

# How long an adopted offset waits for its page's socket, seconds.
ADOPT_CONNECT_TIMEOUT_S = 5.0

# Tools this app has pushed a TCP offset for since it started. Until then a
# controller reporting zero may simply never have been told; afterwards a zero
# is a deliberate clear by whoever set it, and the GUI follows it rather than
# overwriting it with what the browser remembers.
_pushed_offset_tools: set[str] = set()

# The X/Y/Z inputs of one tool's TCP offset row.
OffsetInputs = tuple[ui.number, ...]
TCP_AXES = ("x", "y", "z", "roll", "pitch", "yaw")


def adopt_applied_tcp(calibration: TcpCalibration) -> None:
    """Publish a confirmed controller transform to storage and local kinematics."""
    values = dict(zip(TCP_AXES, calibration.values))
    ng_app.storage.general[f"tcp_offset_{calibration.tool_key}"] = {
        **values,
        "variant_key": calibration.variant_key,
    }
    ng_app.storage.general["selected_tool"] = calibration.tool_key
    ng_app.storage.general[f"tool_variant_{calibration.tool_key}"] = (
        calibration.variant_key
    )
    _pushed_offset_tools.add(calibration.tool_key)
    robot = ui_state.active_robot
    kwargs = (
        {"tcp_rotation_rad": tuple(math.radians(v) for v in calibration.values[3:])}
        if robot.has_tcp_transform
        else {}
    )
    robot.set_active_tool(
        calibration.tool_key,
        tcp_offset_m=tuple(v / 1000 for v in calibration.values[:3]),
        variant_key=calibration.variant_key or None,
        **kwargs,
    )
    if ui_state.urdf_scene:
        ui_state.urdf_scene.apply_tool(
            calibration.tool_key, variant_key=calibration.variant_key or None
        )
        ui_state.urdf_scene.refresh_tcp_ball()
    simulation_state.notify_changed()
    try:
        simulation.schedule_debounced_simulation()
    except RuntimeError:
        pass
    for view in list(_settings_views):
        view.show_applied_tcp(calibration)


def get_available_serial_ports() -> list[str]:
    """Detect available serial ports on the system."""
    try:
        import serial.tools.list_ports

        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports]
    except ImportError:
        logger.warning("pyserial not installed - cannot detect serial ports")
        return []
    except OSError as e:
        logger.error("Error detecting serial ports: %s", e)
        return []


@contextmanager
def _setting_row(title: str, description: str):
    """Keep the control visible; explanatory copy is available on hover/focus."""
    with ui.row().classes("settings-row"):
        ui.label(title).classes("settings-label").props("tabindex=0").tooltip(
            description
        )
        yield


# Live Settings views, so a TCP applied from elsewhere (the Setup panel's
# calibration editor) shows in their inputs before the next nudge pushes.
_settings_views: weakref.WeakSet["SettingsContent"] = weakref.WeakSet()


class SettingsContent:
    """Settings content that can be embedded in the control panel."""

    def __init__(self, client: RobotClient) -> None:
        self.client = client
        self._tcp_inputs: tuple[str, OffsetInputs, Client] | None = None
        _settings_views.add(self)
        self._port_select: ui.select | None = None
        self._refresh_timer: ui.timer | None = None
        self._cam_select: ui.select | None = None
        self._cam_refresh_timer: ui.timer | None = None
        self._variant_container: ui.column | None = None
        self._tcp_offset_container: ui.column | None = None
        self._tool_lock = asyncio.Lock()
        self._tcp_pushing = False
        self._tcp_push_next: tuple[str, dict, OffsetInputs, Client, int] | None = None
        # Bumped by every tool change. A push carries the epoch it was
        # queued under, so an edit in flight when the tool changes is
        # dropped rather than re-applied to the tool that replaced it.
        self._tool_epoch = 0

    def _load_preferences(self) -> dict:
        """Load persisted preferences from storage."""
        valid_profiles = ui_state.active_robot.motion_profiles
        stored_profile = ng_app.storage.general.get("motion_profile", "TOPPRA")
        if stored_profile not in valid_profiles and valid_profiles:
            stored_profile = valid_profiles[0]
        return {
            "com_port": ng_app.storage.general.get("com_port", ""),
            "show_route": ng_app.storage.general.get("show_route", True),
            "envelope_mode": EnvelopeMode(
                ng_app.storage.general.get("envelope_mode", "auto")
            ),
            "theme_mode": ng_app.storage.general.get("theme_mode", "system"),
            "motion_profile": stored_profile,
            "jog_blend_r": jog_blend_r(),
            "translation_frame": ng_app.storage.general.get("translation_frame", "WRF"),
            "jog_invert_x": bool(ng_app.storage.general.get("jog_invert_x", False)),
            "jog_invert_y": bool(ng_app.storage.general.get("jog_invert_y", False)),
            "show_divergence": bool(
                ng_app.storage.general.get("show_divergence", True)
            ),
            "show_contacts": bool(ng_app.storage.general.get("show_contacts", False)),
            "show_com": bool(ng_app.storage.general.get("show_com", False)),
        }

    def _refresh_serial_ports(self) -> None:
        """Refresh the available serial ports in the dropdown."""
        if self._port_select:
            ports = get_available_serial_ports()
            self._port_select.options = ports
            self._port_select.update()

    def cleanup(self) -> None:
        """Cancel background timers during shutdown."""
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        if self._cam_refresh_timer is not None:
            self._cam_refresh_timer.cancel()

    # ── Tool helpers ─────────────────────────────────────────────────

    def _get_variant_key(self, tool_key: str) -> str | None:
        """Get stored variant key for a tool, or first variant key."""
        try:
            tool_spec = ui_state.active_robot.tools[tool_key]
        except (KeyError, AttributeError):
            return None
        variants = tool_spec.variants
        if not variants:
            return None
        stored = ng_app.storage.general.get(f"tool_variant_{tool_key}")
        # "" is a stored answer, not a missing one: the controller carries the
        # tool with no variant (a program's select_tool without variant_key).
        if stored is not None and (
            stored == "" or any(v.key == stored for v in variants)
        ):
            return stored
        return variants[0].key

    def _bound_variant(self, tool_key: str) -> str:
        """The variant a TCP edit for ``tool_key`` must be bound to: the
        controller's own when it carries that tool, else the browser's."""
        tool = waldoctl.commander.status.tool
        if tool.key == tool_key:
            return tool.variant_key or ""
        return self._get_variant_key(tool_key) or ""

    def _get_tcp_offset(self, tool_key: str) -> dict:
        """Get stored TCP offset for a tool (mm)."""
        stored = ng_app.storage.general.get(f"tcp_offset_{tool_key}", {})
        variant = self._get_variant_key(tool_key) or ""
        if stored.get("variant_key", variant) != variant:
            return {axis: 0.0 for axis in TCP_AXES}
        return {axis: stored.get(axis, 0.0) for axis in TCP_AXES}

    def _tcp_offset_m(self, tool_key: str) -> tuple[float, float, float] | None:
        """Get stored TCP offset in meters, or None if zero."""
        o = self._get_tcp_offset(tool_key)
        x, y, z = o.get("x", 0), o.get("y", 0), o.get("z", 0)
        if x == 0 and y == 0 and z == 0:
            return None
        return (x / 1000, y / 1000, z / 1000)

    def _notify_and_resimulate(self) -> None:
        """Notify simulation state changed and trigger debounced re-simulation."""
        simulation_state.notify_changed()
        try:
            simulation.schedule_debounced_simulation()
        except RuntimeError:
            pass

    def _apply_tool_scene(self, tool_key: str, variant_key: str | None = None) -> None:
        """Apply tool to local FK/IK model and 3D scene."""
        ui_state.active_robot.set_active_tool(
            tool_key,
            tcp_offset_m=self._tcp_offset_m(tool_key),
            variant_key=variant_key,
            **(
                {
                    "tcp_rotation_rad": tuple(
                        math.radians(float(self._get_tcp_offset(tool_key).get(k, 0)))
                        for k in TCP_AXES[3:]
                    )
                }
                if ui_state.active_robot.has_tcp_transform
                else {}
            ),
        )
        if ui_state.urdf_scene:
            ui_state.urdf_scene.apply_tool(tool_key, variant_key=variant_key)
            ui_state.urdf_scene.refresh_tcp_ball()

    def _rebuild_variant_selector(self, tool_key: str) -> None:
        """Rebuild variant sub-selector for the current tool."""
        assert self._variant_container is not None
        self._variant_container.clear()
        try:
            tool_spec = ui_state.active_robot.tools[tool_key]
        except (KeyError, AttributeError):
            tool_spec = None
        variants = tool_spec.variants if tool_spec else ()
        is_none = tool_key == "NONE"
        self._variant_container.set_visibility(bool(variants) and not is_none)
        if not variants:
            return

        variant_options = (
            {v.key: v.display_name for v in variants} if variants else {"": "—"}
        )
        current_vk = self._get_variant_key(tool_key)
        if current_vk is None:
            current_vk = next(iter(variant_options), "") if variants else ""
        if current_vk == "":
            variant_options = {"": "—", **variant_options}

        async def _on_variant_change(e):
            async with self._tool_lock:
                vk = e.value
                self._tool_epoch += 1
                self._tcp_push_next = None
                try:
                    index = await self.client.select_tool(
                        tool_key, variant_key=vk or ""
                    )
                    if index < 0 or not await self.client.wait_command(
                        index, timeout=5.0
                    ):
                        raise TimeoutError("Tool variant change was not confirmed")
                except Exception as exc:
                    logger.warning("Tool variant change failed: %s", exc)
                    ui.notify(f"Tool variant change failed: {exc}", color="negative")
                    return
                ng_app.storage.general[f"tool_variant_{tool_key}"] = vk
                ng_app.storage.general[f"tcp_offset_{tool_key}"] = {
                    **dict.fromkeys(TCP_AXES, 0.0),
                    "variant_key": vk or "",
                }
                waldoctl.commander.status.tool.variant_key = vk or ""
                self._apply_tool_scene(tool_key, variant_key=vk)
                self._rebuild_tcp_offset(tool_key)
                self._notify_and_resimulate()

        with self._variant_container:
            with _setting_row("Variant", "Tool configuration variant"):
                sel = (
                    ui.select(
                        options=variant_options,
                        value=current_vk,
                        on_change=_on_variant_change,
                    )
                    .classes("w-32")
                    .props("dense")
                    .mark("select-tool-variant")
                )
                if is_none or not variants:
                    sel.props("disable")

    def _rebuild_tcp_offset(
        self, tool_key: str, *, tool_changed: bool = False, adopt_only: bool = False
    ) -> None:
        assert self._tcp_offset_container is not None
        self._tcp_offset_container.clear()
        full = ui_state.active_robot.has_tcp_transform
        disabled = tool_key == "NONE" and not full
        offset = self._get_tcp_offset(tool_key)
        axes = TCP_AXES if full else TCP_AXES[:3]
        page_client = context.client
        inputs: OffsetInputs = ()

        async def _on_offset_change(_e=None):
            if any(item.value is None for item in inputs):
                return
            vals = {axis: item.value for axis, item in zip(axes, inputs)}
            await self._push_tcp_offset(tool_key, vals, inputs, page_client)

        with self._tcp_offset_container:
            with _setting_row(
                "TCP Offset",
                "Tool-local mm / intrinsic XYZ degrees"
                if full
                else "Offset from default TCP (mm)",
            ):
                with ui.grid(columns=3).classes("gap-1"):
                    inputs = tuple(
                        ui.number(
                            label=axis.upper(), value=offset.get(axis, 0), step=0.5
                        )
                        .classes("w-16")
                        .props("dense borderless" + (" disable" if disabled else ""))
                        .on(
                            "update:model-value",
                            _on_offset_change,
                            throttle=TCP_EDIT_THROTTLE_S,
                            leading_events=False,
                        )
                        .mark(f"tcp-offset-{axis}")
                        for axis in axes
                    )
        self._tcp_inputs = (tool_key, inputs, page_client)
        if not disabled:
            background_tasks.create(
                self._reconcile_tcp_offset(
                    tool_key,
                    inputs,
                    page_client,
                    tool_changed=tool_changed,
                    adopt_only=adopt_only,
                    epoch=self._tool_epoch,
                ),
                name="tcp-offset-reconcile",
            )

    def show_applied_tcp(self, calibration: TcpCalibration) -> None:
        """Reflect a transform the controller confirmed for the shown tool."""
        if self._tcp_inputs is None:
            return
        tool_key, inputs, page_client = self._tcp_inputs
        if tool_key != calibration.tool_key or not page_client.has_socket_connection:
            return
        with page_client:
            for inp, value in zip(inputs, calibration.values):
                if inp.value != value:
                    inp.set_value(value)

    @staticmethod
    def _notify(page_client: Client, message: str, color: str) -> None:
        """A toast raised from a background task needs its page's slot stack;
        without one ``ui.notify`` raises and the user is never told."""
        if page_client.has_socket_connection:
            with page_client:
                ui.notify(message, color=color)

    async def _push_tcp_offset(
        self,
        tool_key: str,
        vals: dict,
        inputs: OffsetInputs,
        page_client: Client,
    ) -> None:
        """Send the offset to the controller, newest values only.

        One push runs at a time: overlapping pushes race each other's
        readbacks, and the loser adopts an intermediate value the user has
        already typed past."""
        self._tcp_push_next = (tool_key, vals, inputs, page_client, self._tool_epoch)
        if self._tcp_pushing:
            return
        self._tcp_pushing = True
        try:
            while self._tcp_push_next is not None:
                pending = self._tcp_push_next
                self._tcp_push_next = None
                await self._send_tcp_offset(*pending)
        finally:
            self._tcp_pushing = False

    async def _read_tcp(self) -> list[float]:
        if ui_state.active_robot.has_tcp_transform:
            values = await self.client.tcp_transform()
        else:
            values = [*(await self.client.tcp_offset()), 0.0, 0.0, 0.0]
        return list(TcpCalibration(cast(PoseValues, tuple(values)), "readback").values)

    async def _send_tcp_offset(
        self,
        tool_key: str,
        vals: dict,
        inputs: OffsetInputs,
        page_client: Client,
        epoch: int,
    ) -> None:
        async with self._tool_lock:
            await self._send_tcp_offset_locked(
                tool_key, vals, inputs, page_client, epoch
            )

    async def _send_tcp_offset_locked(
        self,
        tool_key: str,
        vals: dict,
        inputs: OffsetInputs,
        page_client: Client,
        epoch: int,
    ) -> None:
        if epoch != self._tool_epoch:
            return
        try:
            values = cast(PoseValues, tuple(float(vals.get(k, 0)) for k in TCP_AXES))
            calibration = TcpCalibration(
                values, tool_key, self._bound_variant(tool_key)
            )
            if ui_state.active_robot.has_tcp_transform:
                from waldo_commander.services.tcp_calibration import (
                    apply_tcp_calibration,
                )

                await apply_tcp_calibration(self.client, calibration)
            else:
                index = await self.client.set_tcp_offset(*values[:3])
                if index < 0 or not await self.client.wait_command(index, timeout=15.0):
                    raise TimeoutError("TCP offset application was not confirmed")
            back = await self._read_tcp()
            if epoch != self._tool_epoch:
                return
            await self._adopt_tcp_offset(tool_key, back, inputs, page_client)
        except Exception as exc:
            logger.warning("TCP transform was not applied: %s", exc)
            self._notify(page_client, f"TCP transform not applied: {exc}", "negative")

    async def _reconcile_tcp_offset(
        self,
        tool_key: str,
        inputs: OffsetInputs,
        page_client: Client,
        *,
        tool_changed: bool,
        adopt_only: bool = False,
        epoch: int,
    ) -> None:
        """Line the browser's remembered offset up with the controller's.

        The controller's offset is the source of truth wherever it can be
        someone else's — a program or another client, and a deliberate zero
        counts. The remembered offset is pushed only where the controller's
        cannot be: right after a tool change, which resets it, and on a
        controller reporting nothing that this app has never told."""
        async with self._tool_lock:
            if epoch != self._tool_epoch:
                return
            try:
                back = await self._read_tcp()
            except Exception as exc:
                logger.debug("tcp_offset readback failed: %s", exc)
                return
            stored = self._get_tcp_offset(tool_key)
            if adopt_only:
                await self._adopt_tcp_offset(tool_key, back, inputs, page_client)
                return
            mine = [float(stored.get(k, 0) or 0) for k in TCP_AXES]
            if all(abs(b - m) <= 1e-3 for b, m in zip(back, mine)):
                return
            never_told = tool_key not in _pushed_offset_tools and not any(
                abs(b) > 1e-3 for b in back
            )
            if tool_changed or never_told:
                await self._send_tcp_offset_locked(
                    tool_key, stored, inputs, page_client, epoch
                )
            else:
                await self._adopt_tcp_offset(tool_key, back, inputs, page_client)

    async def _adopt_tcp_offset(
        self,
        tool_key: str,
        offset_mm: list | tuple,
        inputs: OffsetInputs,
        page_client: Client,
    ) -> None:
        values = cast(PoseValues, tuple(float(v) for v in offset_mm))
        calibration = TcpCalibration(values, tool_key, self._bound_variant(tool_key))
        adopt_applied_tcp(calibration)
        if not page_client.has_socket_connection:
            # The reconcile that adopts an out-of-band offset is started
            # while the page is still being built, so its socket is often
            # not up yet. Dropping the update here would leave the inputs
            # showing the browser's remembered offset while the controller
            # plans with another one.
            try:
                await page_client.connected(timeout=ADOPT_CONNECT_TIMEOUT_S)
            except ClientConnectionTimeout:
                return
        with page_client:
            for inp, v in zip(inputs, values):
                if inp.value != v:
                    inp.set_value(v)
            self._apply_tool_scene(
                tool_key, variant_key=self._get_variant_key(tool_key)
            )
            self._notify_and_resimulate()

    # ── Section builders ─────────────────────────────────────────────

    def _build_serial_port(self, prefs: dict) -> None:
        available_ports = get_available_serial_ports()
        stored_port = prefs["com_port"]

        with _setting_row("Serial Port", "Select robot communication port"):
            self._port_select = (
                ui.select(
                    options=available_ports,
                    value=stored_port if stored_port in available_ports else None,
                    label="Port",
                    new_value_mode="add-unique",
                    clearable=True,
                )
                .classes("w-32")
                .props("dense")
            )

        if stored_port and stored_port not in available_ports:
            self._port_select.value = stored_port

        port_select_ref = self._port_select

        async def _apply_port():
            port_val = port_select_ref.value or ""
            try:
                await self.client.connect_hardware(port_val)
            except Exception as exc:
                logger.warning("connect_hardware(%s) failed: %s", port_val, exc)
                ui.notify(f"Port change failed: {exc}", color="negative")
                return
            ng_app.storage.general["com_port"] = port_val
            ui.notify(f"SET_PORT {port_val}", color="primary")

        port_select_ref.on("update:model-value", lambda e: _apply_port())
        self._refresh_timer = ui.timer(10.0, self._refresh_serial_ports)

    def _build_show_route(self, prefs: dict) -> None:
        async def _on_show_route_change(e):
            val = bool(e.value)
            waldoctl.commander.settings.view.paths_visible = val
            ng_app.storage.general["show_route"] = val
            simulation_state.notify_changed()

        with _setting_row("Show Route", "Display path visualization in 3D view"):
            ui.switch(
                value=prefs["show_route"],
                on_change=_on_show_route_change,
            ).props("dense").mark("switch-show-route")

        waldoctl.commander.settings.view.paths_visible = prefs["show_route"]

    def _build_physics_overlays(self, prefs: dict) -> None:
        """What the simulated run measured, drawn over the scene.

        Only meaningful on a backend that simulates; the section says so
        rather than hiding, because "my robot has no physics" is worth
        knowing and a hidden control is not.
        """
        view = waldoctl.commander.settings.view
        simulates = ui_state.active_robot.has_physics_simulation

        def toggle(label: str, hint: str, key: str, attr: str, marker: str) -> None:
            async def _on_change(e):
                setattr(view, attr, bool(e.value))
                ng_app.storage.general[key] = bool(e.value)
                simulation_state.notify_changed()

            with _setting_row(label, hint):
                ui.switch(value=prefs[key], on_change=_on_change).props("dense").mark(
                    marker
                ).set_enabled(simulates)
            setattr(view, attr, prefs[key])

        toggle(
            "Achieved Path",
            "Draw where the arm ends up beside where it is aimed",
            "show_divergence",
            "divergence_visible",
            "switch-show-divergence",
        )
        toggle(
            "Contacts",
            "Contact points and the forces through them",
            "show_contacts",
            "contacts_visible",
            "switch-show-contacts",
        )
        toggle(
            "Centre of Mass",
            "The scene's centre of mass and its drop line",
            "show_com",
            "com_visible",
            "switch-show-com",
        )

    def _build_envelope(self, prefs: dict) -> None:
        async def _on_envelope_mode_change(e):
            mode = EnvelopeMode(e.value)
            waldoctl.commander.settings.view.envelope_mode = mode
            ng_app.storage.general["envelope_mode"] = mode.value
            simulation_state.notify_changed()

        with _setting_row("Workspace Envelope", "Show reachable workspace boundary"):
            ui.select(
                options={m.value: m.value.capitalize() for m in EnvelopeMode},
                value=prefs["envelope_mode"].value,
                on_change=_on_envelope_mode_change,
            ).classes("w-24").props("dense").mark("select-envelope-mode")

        waldoctl.commander.settings.view.envelope_mode = prefs["envelope_mode"]

    def _build_tool_section(self) -> None:
        synchronizing = False
        pending_changes = 0

        async def change_tool(e):
            nonlocal pending_changes
            try:
                await _on_tool_change(e)
            finally:
                pending_changes -= 1

        def request_tool_change(e):
            nonlocal pending_changes
            if synchronizing:
                return None
            pending_changes += 1
            return change_tool(e)

        async def _on_tool_change(e):
            async with self._tool_lock:
                tool = e.value
                self._tool_epoch += 1
                self._tcp_push_next = None
                vk = self._get_variant_key(tool)
                try:
                    index = await self.client.select_tool(tool, variant_key=vk or "")
                    if index < 0 or not await self.client.wait_command(
                        index, timeout=5.0
                    ):
                        raise TimeoutError("Tool change was not confirmed")
                except Exception as exc:
                    logger.warning("select_tool(%s) failed: %s", tool, exc)
                    ui.notify(f"Tool change failed: {exc}", color="negative")
                    return

                ng_app.storage.general["selected_tool"] = tool
                waldoctl.commander.status.tool.variant_key = vk or ""
                self._apply_tool_scene(tool, variant_key=vk)
                self._apply_tool_camera(tool)
                self._rebuild_variant_selector(tool)
                self._rebuild_tcp_offset(tool, tool_changed=True)
                self._notify_and_resimulate()

        tool_options = {}
        for tool in ui_state.active_robot.tools.available:
            tool_options[tool.key] = tool.display_name.replace("_", " ")

        default_tool = next(iter(tool_options), "NONE")
        stored_tool = ng_app.storage.general.get("selected_tool", default_tool)
        if stored_tool not in tool_options:
            stored_tool = default_tool

        with _setting_row("Tool", "Select end effector tool"):
            tool_select = (
                ui.select(
                    options=tool_options,
                    value=stored_tool,
                    on_change=request_tool_change,
                )
                .classes("w-40")
                .props("dense")
                .mark("select-tool")
            )

        self._variant_container = ui.column().classes("w-full gap-1")
        self._rebuild_variant_selector(stored_tool)

        with (
            ui.expansion("TCP offset", icon="tune")
            .classes("w-full")
            .mark("settings-tcp-details")
        ):
            ui.label("Edits apply to the controller.").classes("panel-note")
            self._tcp_offset_container = ui.column().classes("w-full gap-1")
            self._rebuild_tcp_offset(stored_tool)

        vk_initial = self._get_variant_key(stored_tool)
        waldoctl.commander.status.tool.variant_key = vk_initial or ""
        if stored_tool:
            self._apply_tool_scene(stored_tool, variant_key=vk_initial)

        def sync_tool() -> None:
            nonlocal synchronizing
            status = waldoctl.commander.status
            tool = status.tool.key
            if (
                pending_changes
                or self._tool_lock.locked()
                or not (status.connected or status.simulator_active)
            ):
                return
            variant = status.tool.variant_key or ""
            if tool not in tool_options or (
                tool == tool_select.value
                and variant == (self._get_variant_key(tool) or "")
            ):
                return
            # Status changes can come from another client. Reflect them without
            # sending SELECT_TOOL or restoring an old browser TCP correction.
            self._tool_epoch += 1
            self._tcp_push_next = None
            ng_app.storage.general["selected_tool"] = tool
            ng_app.storage.general[f"tool_variant_{tool}"] = status.tool.variant_key
            synchronizing = True
            try:
                tool_select.set_value(tool)
            finally:
                synchronizing = False
            self._rebuild_variant_selector(tool)
            self._rebuild_tcp_offset(tool, adopt_only=True)
            self._apply_tool_camera(tool)

        ui.timer(0.3, sync_tool)

    def _tool_spec(self, tool_key: str):
        """The active robot's ToolSpec for *tool_key*, or None."""
        try:
            return ui_state.active_robot.tools[tool_key]
        except KeyError:
            return None

    def _apply_tool_camera(self, tool_key: str) -> None:
        """Resolve the active tool's camera (its runtime_settings override or the
        spec default), start/stop the camera service, and sync the dropdown.
        Treats None / -1 as 'no camera'."""
        spec = self._tool_spec(tool_key)
        if spec is None:
            return
        stored = ng_app.storage.general.get(f"tool_camera/{tool_key}")
        if stored is not None:
            spec.runtime_settings.camera_device = None if stored == -1 else stored
        device = spec.effective_camera_device
        cam_value = -1 if device is None else device
        if self._cam_select is not None:
            self._cam_select.value = cam_value
        ui_state.camera_device = cam_value
        if device is None or device == -1:
            camera_service.stop()
        else:
            camera_service.start(device)

    def _build_camera(self) -> None:
        cam_devices = enumerate_video_devices()
        cam_options: dict[int | str, str] = {-1: "Disabled"}
        for dev in cam_devices:
            cam_options[dev["index"]] = str(dev["label"])

        # Per-tool camera: the dropdown reflects / sets the *active* tool's camera
        # (its runtime_settings override, falling back to the tool spec default).
        active_key = ng_app.storage.general.get("selected_tool", "NONE")
        spec = self._tool_spec(active_key)
        if spec is not None:
            stored = ng_app.storage.general.get(f"tool_camera/{active_key}")
            if stored is not None:
                spec.runtime_settings.camera_device = None if stored == -1 else stored
        device = spec.effective_camera_device if spec is not None else -1
        cam_value: int | str = -1 if device is None else device
        if cam_value not in cam_options:
            cam_value = -1
        ui_state.camera_device = cam_value

        def _on_camera_change(e):
            val = e.value
            key = ng_app.storage.general.get("selected_tool", "NONE")
            s = self._tool_spec(key)
            if s is not None:
                s.runtime_settings.camera_device = (
                    None if (val is None or val == -1) else val
                )
                ng_app.storage.general[f"tool_camera/{key}"] = (
                    -1 if val is None else val
                )
            ui_state.camera_device = val
            if val is None or val == -1:
                camera_service.stop()
            else:
                camera_service.start(val)

        with _setting_row("Camera", "Video device for the active tool"):
            self._cam_select = (
                ui.select(
                    options=cam_options,
                    value=cam_value,
                    on_change=_on_camera_change,
                    new_value_mode="add-unique",
                    clearable=True,
                )
                .classes("w-32")
                .props("dense")
                .mark("select-camera")
            )

        with ui.expansion("Virtual camera help", icon="help_outline").classes("w-full"):
            ui.label(
                "AI annotations: webcam \u2192 your script \u2192 pyvirtualcam \u2192 select virtual device"
            ).classes("text-xs text-gray-500 dark:text-gray-400")
            ui.label("Linux: sudo apt install v4l2loopback-dkms").classes(
                "text-xs text-gray-500 dark:text-gray-400"
            )

        def _refresh_camera_devices() -> None:
            if self._cam_select:
                new_devices = enumerate_video_devices()
                new_options: dict[int | str, str] = {-1: "Disabled"}
                for dev in new_devices:
                    new_options[dev["index"]] = str(dev["label"])
                # Keep any custom entries the user typed
                for k, v in self._cam_select.options.items():  # ty: ignore[unresolved-attribute]
                    if k not in new_options and k != -1:
                        new_options[k] = v
                self._cam_select.options = new_options
                self._cam_select.update()

        self._cam_refresh_timer = ui.timer(10.0, _refresh_camera_devices)

        if cam_value != -1:
            camera_service.start(cam_value)

    def _build_motion_profile(self, prefs: dict) -> None:
        async def _on_motion_profile_change(e):
            profile = e.value
            try:
                await self.client.select_profile(profile)
            except Exception as exc:
                logger.warning("select_profile(%s) failed: %s", profile, exc)
                ui.notify(f"Profile change failed: {exc}", color="negative")
                return
            ng_app.storage.general["motion_profile"] = profile

        motion_profile_options = {}
        for p in ui_state.active_robot.motion_profiles:
            motion_profile_options[p] = p.replace("_", " ").title()

        with _setting_row("Motion Profile", "Trajectory generation algorithm"):
            ui.select(
                options=motion_profile_options,
                value=prefs["motion_profile"],
                on_change=_on_motion_profile_change,
            ).classes("w-32").props("dense").mark("select-motion-profile")

    def _build_theme(self, prefs: dict) -> None:
        with _setting_row("Theme", "Application color scheme"):
            with ui.element("span").tooltip(
                "Light mode will be available in a future update"
            ):
                ui.select(
                    options={"dark": "Dark"},
                    value="dark",
                ).classes("w-24").props("dense disable")

    def _build_backend_selector(self) -> None:
        """Backend (robot driver) selection dropdown.

        Writes the chosen backend name to
        ``commander.settings.plugins.backend`` and shows a "restart
        required" hint — backend switching takes effect on next launch.
        """
        from waldoctl.discovery import available_backends

        installed = sorted(available_backends())
        if not installed:
            return  # Defensive: shouldn't happen since startup already resolved one
        plugins = waldoctl.commander.settings.plugins
        active = ui_state.active_robot.name.lower()
        current = plugins.backend or active
        if current not in installed:
            current = installed[0]

        def _on_backend_change(e) -> None:
            new = e.value
            plugins.backend = new
            ng_app.storage.general["plugins/backend"] = new
            self._restart_notice.set_text(
                f"Restart to use {new.upper()}. Active: {active.upper()}."
            )
            self._restart_notice.set_visibility(new != active)

        with _setting_row("Backend", "Robot driver (applied on next launch)"):
            ui.select(
                options={b: b for b in installed},
                value=current,
                on_change=_on_backend_change,
            ).classes("w-40").props("dense").mark("settings-backend-select")

        self._restart_notice = (
            ui.label(f"Restart to use {current.upper()}. Active: {active.upper()}.")
            .classes("panel-note")
            .mark("settings-restart-notice")
        )
        self._restart_notice.set_visibility(current != active)

    def _build_plugin_panels(self) -> None:
        """Panel-enable/disable toggle list.

        One row per installed panel plugin (discovered via the
        ``waldoctl.panels`` entry-point group). Toggling a row writes
        through to ``commander.settings.plugins.disabled_panels`` and the
        rehydrated ``app.storage.general`` slot; the change takes effect on
        the next process start (panels are process-scoped singletons). Stale
        ids — disabled entries whose plugin is no longer installed — are
        stripped on each rebuild.
        """
        from waldoctl.discovery import list_panels

        plugins = waldoctl.commander.settings.plugins
        discovered = iter_plugin_panels()
        # "Known" = loaded plugin ids plus every installed entry-point name, so a
        # disabled plugin that is installed but currently fails to import keeps its
        # disabled choice; only genuinely-uninstalled ids are purged. (ep.name and
        # cls.id usually coincide; a broken plugin whose id differs from its
        # entry-point name is the one residual gap — its id can't be read without
        # importing it.)
        known_ids = {cls.id for cls in discovered} | set(list_panels())

        stale = [pid for pid in plugins.disabled_panels if pid not in known_ids]
        if stale:
            plugins.disabled_panels = [
                pid for pid in plugins.disabled_panels if pid in known_ids
            ]
            ng_app.storage.general["plugins/disabled_panels"] = list(
                plugins.disabled_panels
            )

        if not discovered:
            with _setting_row("Panel plugins", "Show / hide installed panel plugins"):
                ui.label("No plugins installed").classes(
                    "text-xs text-[var(--ctk-muted)]"
                ).mark("settings-plugins-summary")
            return

        def _on_toggle(panel_id: str):
            def _handler(e):
                enabled = bool(e.value)
                current = list(plugins.disabled_panels)
                if enabled:
                    current = [pid for pid in current if pid != panel_id]
                elif panel_id not in current:
                    current.append(panel_id)
                plugins.disabled_panels = current
                ng_app.storage.general["plugins/disabled_panels"] = current
                ui.notify("Restart to apply", color="info")

            return _handler

        for cls in discovered:
            # Skip panels that can never mount (id collides with a core tab) — a
            # toggle for them would be a no-op. (applies_to() is context-dependent
            # — a panel that doesn't apply to this robot still gets a toggle.)
            if cls.id in RESERVED_TAB_IDS:
                continue
            panel_id = cls.id
            label = cls.display_name
            with _setting_row(label, f"Plugin: {panel_id}"):
                ui.switch(
                    value=panel_id not in plugins.disabled_panels,
                    on_change=_on_toggle(panel_id),
                ).props("dense").mark(f"settings-plugin-{panel_id}")

    def _build_plugin_settings(self) -> None:
        """Render settings for each enabled panel plugin that contributes any.
        Owns its leading separator so nothing dangles when no plugin does."""
        commander = waldoctl.commander
        contributors = [
            p
            for p in ui_state.plugin_panels
            if type(p).build_settings is not Panel.build_settings
        ]
        for panel in contributors:
            ui.separator().classes("my-1")
            ui.label(panel.display_name).classes("text-sm font-medium").mark(
                f"settings-plugin-{panel.id}-header"
            )
            # A plugin's build_settings() must not break the whole settings page.
            try:
                panel.build_settings(commander)
            except Exception as e:
                logger.warning("Plugin %s build_settings failed: %s", panel.id, e)

    def _build_mcp_server(self) -> None:
        """MCP server controls.

        ``enabled``, ``host``, and ``port`` bind at server start, so we notify
        "Restart to apply" when those change. On a trusted LAN the server
        runs over plain HTTP with no auth — single-controller arbitration is
        the control lease (take_control), not a token.
        """
        mcp = waldoctl.commander.settings.mcp

        # host / port commit on blur / enter (DOM "change"), not per keystroke,
        # so a half-typed address is never persisted; every handler dirty-checks
        # so an unchanged commit writes nothing and shows no toast.
        def _on_enabled_change(e):
            val = bool(e.value)
            if val == mcp.enabled:
                return
            mcp.enabled = val
            ng_app.storage.general["mcp/enabled"] = val
            ui.notify("Restart to apply", color="info")

        def _on_host_change(e):
            host = (e.args or "").strip() or "127.0.0.1"
            if host == mcp.host:
                return
            mcp.host = host
            ng_app.storage.general["mcp/host"] = host
            ui.notify("Restart to apply", color="info")

        def _on_port_change(e):
            try:
                port = int(e.args)
            except (TypeError, ValueError):
                return
            if not (1 <= port <= 65535) or port == mcp.port:
                return
            mcp.port = port
            ng_app.storage.general["mcp/port"] = port
            ui.notify("Restart to apply", color="info")

        with _setting_row(
            "MCP server", "Expose commander.* to an MCP client (restart to apply)"
        ):
            ui.switch(value=mcp.enabled, on_change=_on_enabled_change).props(
                "dense"
            ).mark("settings-mcp-enabled")

        with _setting_row(
            "MCP host", "Bind address — 127.0.0.1 (local) or a LAN address / 0.0.0.0"
        ):
            ui.input(value=mcp.host).classes("w-40").props("dense").on(
                "change", _on_host_change
            ).mark("settings-mcp-host")

        with _setting_row("MCP port", "Listening port for streamable HTTP"):
            ui.number(value=mcp.port, min=1, max=65535).classes("w-24").props(
                "dense"
            ).on("change", _on_port_change).mark("settings-mcp-port")

    def _build_automation(self) -> None:
        """Hardware I/O automation: cycle-start input and at-home output."""

        def _on_cycle_start_change(e):
            val = bool(e.value)
            automation_state.cycle_start_enabled = val
            ng_app.storage.general["automation/cycle_start"] = val

        with _setting_row(
            "Start program on Input 1",
            "Rising edge runs the active program (robot homed, e-stop clear, "
            "nothing already running)",
        ):
            ui.switch(
                value=automation_state.cycle_start_enabled,
                on_change=_on_cycle_start_change,
            ).props("dense").mark("switch-cycle-start")

        def _on_home_output_change(e):
            val = bool(e.value)
            automation_state.home_output_enabled = val
            ng_app.storage.general["automation/home_output"] = val

        with _setting_row(
            "Home position output",
            "Output 2 turns on while all joints are within tolerance of the "
            "home/standby pose",
        ):
            ui.switch(
                value=automation_state.home_output_enabled,
                on_change=_on_home_output_change,
            ).props("dense").mark("switch-home-output")

        def _on_tolerance_change(e):
            try:
                tol = float(e.value)
            except (TypeError, ValueError):
                return
            if not (0.1 <= tol <= 45.0):
                return
            automation_state.home_tolerance_deg = tol
            ng_app.storage.general["automation/home_tolerance_deg"] = tol

        with _setting_row(
            "Home tolerance (deg)", "Joint distance from home treated as at-home"
        ):
            ui.number(
                value=automation_state.home_tolerance_deg,
                min=0.1,
                max=45,
                step=0.1,
                on_change=_on_tolerance_change,
            ).classes("w-24").props("dense").mark("input-home-tolerance")

    def _build_reference_frames(self, prefs: dict) -> None:
        cp = ui_state.control_panel
        frames = ui_state.active_robot.cartesian_frames
        trf_available = (
            len(frames) > 1
            and waldoctl.commander.status.pose.cart_jog.by_frame.get(frames[1])
            is not None
        )
        value = prefs["translation_frame"]
        if value == "TRF" and not trf_available:
            value = "WRF"

        def _on_translation_frame_change(e):
            cp.set_translation_frame(e.value)
            ng_app.storage.general["translation_frame"] = e.value

        with _setting_row("Translation RF", "Reference frame for translation moves"):
            sel = (
                ui.select(
                    options={"WRF": "World", "TRF": "Tool"},
                    value=value,
                    on_change=_on_translation_frame_change,
                )
                .classes("w-24")
                .props("dense")
                .mark("select-translation-frame")
            )
            if not trf_available:
                sel.props("disable").tooltip(
                    "Tool-frame jogging is unavailable for this robot"
                )

        ui.separator().classes("my-1")

        with _setting_row("Rotation RF", "Reference frame for rotation moves"):
            with ui.element("span").tooltip("Rotation always jogs in the tool frame"):
                ui.select(
                    options={"WRF": "World", "TRF": "Tool"},
                    value="TRF",
                ).classes("w-24").props("dense disable")

    def _build_blend_radius(self, prefs: dict) -> None:
        def _on_blend_r_change(e):
            if e.value is None:
                return
            ng_app.storage.general["jog_blend_r"] = max(
                0.0, min(JOG_BLEND_R_MAX, float(e.value))
            )

        with _setting_row(
            "Blend Radius", "Corner smoothing for generated moves (0 = exact stop)"
        ):
            ui.number(
                value=prefs["jog_blend_r"],
                min=0,
                max=JOG_BLEND_R_MAX,
                step=1,
                suffix="mm",
                on_change=_on_blend_r_change,
            ).classes("w-24").props("dense").mark("settings-blend-radius")

    def _build_jog_inversion(self, prefs: dict) -> None:
        cp = ui_state.control_panel

        def _on_invert_x(e):
            val = bool(e.value)
            cp.set_jog_inversion(invert_x=val)
            ng_app.storage.general["jog_invert_x"] = val

        def _on_invert_y(e):
            val = bool(e.value)
            cp.set_jog_inversion(invert_y=val)
            ng_app.storage.general["jog_invert_y"] = val

        with _setting_row("Invert X Jog", "Flip the X jog direction (arrows and A/D)"):
            ui.switch(value=prefs["jog_invert_x"], on_change=_on_invert_x).props(
                "dense"
            ).mark("switch-invert-x")

        ui.separator().classes("my-1")

        with _setting_row("Invert Y Jog", "Flip the Y jog direction (arrows and W/S)"):
            ui.switch(value=prefs["jog_invert_y"], on_change=_on_invert_y).props(
                "dense"
            ).mark("switch-invert-y")

    # ── Main entry point ─────────────────────────────────────────────

    def build_embedded(
        self, ai_control_section: Callable[[], None] | None = None
    ) -> None:
        """Build the settings content for embedding in control panel.

        ``ai_control_section`` is the control panel's AI mode row, slotted in
        with the other AI/MCP settings so hardware settings stay on top.
        """
        prefs = self._load_preferences()

        with ui.column().classes("settings-content"):
            category = (
                ui.select(
                    ["Robot", "Jog", "View", "Panels", "AI & Automation"], value="Robot"
                )
                .props('dense outlined aria-label="Settings category"')
                .classes("settings-category")
                .mark("settings-category")
            )
            with ui.column().classes("panel-body gap-1"):
                with (
                    ui.column()
                    .classes("settings-group")
                    .bind_visibility_from(category, "value", value="Robot")
                ):
                    self._build_backend_selector()
                    if ui_state.active_robot.name.lower() == "parol6":
                        self._build_serial_port(prefs)
                    self._build_tool_section()
                    with ui.expansion("Camera", icon="videocam").classes("w-full"):
                        self._build_camera()
                with (
                    ui.column()
                    .classes("settings-group")
                    .bind_visibility_from(category, "value", value="Jog")
                ):
                    self._build_reference_frames(prefs)
                    self._build_motion_profile(prefs)
                    with (
                        ui.expansion("Advanced", icon="tune")
                        .classes("w-full")
                        .mark("settings-jog-advanced")
                    ):
                        self._build_jog_inversion(prefs)
                        self._build_blend_radius(prefs)
                with (
                    ui.column()
                    .classes("settings-group")
                    .bind_visibility_from(category, "value", value="View")
                ):
                    self._build_show_route(prefs)
                    self._build_envelope(prefs)
                    with ui.expansion("Physics overlays", icon="layers").classes(
                        "w-full"
                    ):
                        if not ui_state.active_robot.has_physics_simulation:
                            ui.label("Unavailable on this backend.").classes(
                                "panel-note"
                            )
                        self._build_physics_overlays(prefs)
                    with (
                        ui.expansion("Appearance", icon="palette")
                        .classes("w-full")
                        .mark("settings-appearance")
                    ):
                        self._build_theme(prefs)
                with (
                    ui.column()
                    .classes("settings-group")
                    .bind_visibility_from(category, "value", value="Panels")
                ):
                    ui.label("Panel changes apply after restart.").classes("panel-note")
                    self._build_plugin_panels()
                    self._build_plugin_settings()
                with (
                    ui.column()
                    .classes("settings-group")
                    .bind_visibility_from(category, "value", value="AI & Automation")
                ):
                    if ai_control_section:
                        ai_control_section()
                    with ui.expansion("MCP server", icon="lan").classes("w-full"):
                        ui.label("Connection changes apply after restart.").classes(
                            "panel-note"
                        )
                        self._build_mcp_server()
                    with (
                        ui.expansion(
                            "Hardware automation", icon="settings_input_component"
                        )
                        .classes("w-full")
                        .mark("settings-automation")
                    ):
                        self._build_automation()

        simulation_state.notify_changed()
