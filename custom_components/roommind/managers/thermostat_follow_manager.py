"""Follow a setpoint turned on a room's thermostat or AC (VTherm's "follow underlying temp change").

The user's value becomes the room target, held as a manual override until the
active schedule's next change. RoomMind then drives every device of the room again,
including the one that was turned.

A device reports many setpoint changes that are no user's: echoes of what
RoomMind sent (often late and under a new context), firmware frost or window
protection, a boost to the device maximum, valve protection. Each change has to
pass every check below; anything doubtful is ignored and logged at debug level.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ATTR_TARGET_TEMP_HIGH, ATTR_TARGET_TEMP_LOW
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from ..const import (
    CLIMATE_MODE_AUTO,
    CLIMATE_MODE_COOL_ONLY,
    CLIMATE_MODE_HEAT_ONLY,
    DOMAIN,
    FOLLOW_SETTLE_S,
    MAX_TARGET_TEMP,
    MIN_TARGET_TEMP,
    MODE_COOLING,
    MODE_HEATING,
    OVERRIDE_CUSTOM,
    ROOMMIND_CONTEXT_ID,
)
from ..utils.device_utils import get_device_by_eid, get_setpoint_offset, is_trv_type
from ..utils.override_utils import async_store_room_override, band_from_temperature_call, current_band
from ..utils.temp_utils import ha_temp_to_celsius

if TYPE_CHECKING:
    from ..coordinator import RoomMindCoordinator

_LOGGER = logging.getLogger(__name__)

# No adoption right after setup or a reload: devices report their state, the write history is empty
FOLLOW_STARTUP_GRACE_S = 120
# A knob turned 22.5 → 23 → 23.5 adopts once, with the last value
FOLLOW_DEBOUNCE_S = 5.0
# A band within this of the one in force is not a change (RoomMind's own direct-mode setpoint)
_SAME_BAND_TOLERANCE = 0.25
_TEMP_KEYS = (ATTR_TEMPERATURE, ATTR_TARGET_TEMP_LOW, ATTR_TARGET_TEMP_HIGH)
_ACTIVE_HVAC_STATES = ("heat", "cool", "heat_cool", "auto")
HVAC_STATE_OFF = "off"
# A device switched from off or from its own schedule (auto; tado over HomeKit shows it as
# heat_cool) to a fixed mode: turning the dial does that, the new setpoint is the user's
_MANUAL_FROM_STATES = (HVAC_STATE_OFF, "heat_cool", "auto")
_MANUAL_STATES = ("heat", "cool")
_UNUSABLE_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)
_DEFAULT_STEP = 0.5

SIDE_HEAT = "heat"
SIDE_COOL = "cool"


@dataclass(frozen=True)
class _Change:
    """A setpoint change on one device that passed the device checks."""

    entity_id: str
    hvac_state: str
    temps: dict[str, float]  # changed attributes, HA units


def _step(state: State) -> float:
    step = state.attributes.get("target_temp_step")
    return float(step) if isinstance(step, (int, float)) and step > 0 else _DEFAULT_STEP


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _describe(state: State | None) -> str:
    """State and setpoints of a device for the debug log."""
    if state is None:
        return "none"
    temps = {key: state.attributes.get(key) for key in (*_TEMP_KEYS, "preset_mode") if key in state.attributes}
    return f"{state.state} {temps}"


def _ignored(entity_id: str, reason: str) -> _Change | None:
    """Log why a state change is not followed (debug), for `return _ignored(...)`."""
    _LOGGER.debug("Thermostat follow: '%s' not followed: %s", entity_id, reason)
    return None


def changed_setpoints(old: State, new: State, tolerance: float, *, appeared: bool = False) -> dict[str, float]:
    """The setpoint attributes that moved by more than *tolerance* between two states.

    With *appeared*, a setpoint the old state did not report counts as changed: a device
    switched on from off reports none before (tado over HomeKit shows `temperature: None`).
    """
    changed: dict[str, float] = {}
    for key in _TEMP_KEYS:
        before, after = _num(old.attributes.get(key)), _num(new.attributes.get(key))
        if after is None:
            continue
        if (before is None and appeared) or (before is not None and abs(after - before) > tolerance):
            changed[key] = after
    return changed


class ThermostatFollowManager:
    """Turns a setpoint changed on a room's device into the room's target."""

    def __init__(self, hass: HomeAssistant, coordinator: RoomMindCoordinator) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self._started_at: float | None = None
        self._eid_to_area: dict[str, str] = {}
        self._unsub_state: Callable[[], None] | None = None
        self._pending: dict[str, _Change] = {}
        self._debounce: dict[str, Callable[[], None]] = {}
        # Last adopted change per room, for diagnostics: {area_id: {entity_id, value, at}}
        self.last_adoption: dict[str, dict[str, Any]] = {}

    # ─── Lifecycle ────────────────────────────────────────────────

    @callback
    def async_start(self) -> Callable[[], None]:
        """Start following; returns the stop callback."""
        self._started_at = time.monotonic()
        self.async_sync(self.hass.data[DOMAIN]["store"].get_rooms())
        stop: Callable[[], None] = self.async_stop
        return stop

    @callback
    def async_stop(self) -> None:
        self._started_at = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self._eid_to_area = {}
        for cancel in self._debounce.values():
            cancel()
        self._debounce.clear()
        self._pending.clear()

    @callback
    def async_sync(self, rooms: dict[str, dict]) -> None:
        """Follow the devices of the rooms that have it switched on (cheap when nothing changed)."""
        if self._started_at is None:
            return
        wanted: dict[str, str] = {}
        shared: set[str] = set()
        for area_id, room in rooms.items():
            if not room.get("follow_thermostat") or room.get("is_outdoor"):
                continue
            for device in room.get("devices", []):
                eid = device.get("entity_id")
                if not eid:
                    continue
                if eid in wanted and wanted[eid] != area_id:
                    shared.add(eid)
                wanted[eid] = area_id
        for eid in shared:
            # A device in two rooms cannot tell which room the user meant
            _LOGGER.debug("Thermostat follow: '%s' belongs to several rooms, not followed", eid)
            wanted.pop(eid)
        for area_id in set(self._pending) - set(wanted.values()):
            self._drop_pending(area_id)
        if wanted == self._eid_to_area:
            return
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self._eid_to_area = wanted
        _LOGGER.info(
            "Thermostat follow: watching %s",
            ", ".join(f"{eid} ({area})" for eid, area in sorted(wanted.items())) or "no devices",
        )
        if wanted:
            self._unsub_state = async_track_state_change_event(self.hass, list(wanted), self._on_state_change)

    def _drop_pending(self, area_id: str) -> None:
        self._pending.pop(area_id, None)
        cancel = self._debounce.pop(area_id, None)
        if cancel is not None:
            cancel()

    # ─── Device checks ────────────────────────────────────────────

    def _device_change(
        self, entity_id: str, old: State | None, new: State | None, context_parent: str | None
    ) -> _Change | None:
        """The user's setpoint change in this state change, None when it is not one."""
        _LOGGER.debug(
            "Thermostat follow: '%s' %s -> %s",
            entity_id,
            _describe(old),
            _describe(new),
        )
        if old is None or new is None or old.state in _UNUSABLE_STATES or new.state in _UNUSABLE_STATES:
            return None
        mode_change = new.state != old.state
        if new.state not in _ACTIVE_HVAC_STATES and not (old.state == new.state == HVAC_STATE_OFF):
            # Fan/dry hold no setpoint to follow; switched off is no new target
            return _ignored(entity_id, f"mode {old.state} -> {new.state}")
        if mode_change and not (old.state in _MANUAL_FROM_STATES and new.state in _MANUAL_STATES):
            # Heat <-> cool sets its own setpoint on many devices; into auto the device
            # resumes its own schedule and setpoint
            return _ignored(entity_id, f"mode {old.state} -> {new.state}")
        if new.attributes.get("preset_mode") != old.attributes.get("preset_mode"):
            # The device's own boost/eco/away preset, not a target for the room
            return _ignored(entity_id, "device preset changed")
        step = _step(new)
        tolerance = step / 2 + 0.05
        temps = changed_setpoints(old, new, tolerance, appeared=old.state in _MANUAL_FROM_STATES)
        if not temps:
            return _ignored(entity_id, "no setpoint change")
        tracker = self._coordinator.write_tracker
        age = tracker.last_write_age(entity_id)
        # HA stamps an entity's state writes with the last service call's context for 5 s.
        # A device that reports a turned dial only when RoomMind writes next (tado over
        # HomeKit) shows the user's value under RoomMind's context: it counts as settling,
        # and the value comparison below tells the echo from the dial.
        settling = context_parent == ROOMMIND_CONTEXT_ID or (age is not None and age < FOLLOW_SETTLE_S)
        if settling and (mode_change or old.state == HVAC_STATE_OFF):
            # RoomMind idles devices off and switches modes; on its way a device may
            # restore an old setpoint RoomMind no longer remembers
            return _ignored(entity_id, f"{temps} with a mode change right after RoomMind's write")
        if settling:
            # Right after a write the device may still be rounding or settling on it
            tolerance = step + 0.05
        if tracker.matches_recent_write(entity_id, temps, tolerance):
            return _ignored(entity_id, f"{temps} is an echo of RoomMind's write")
        dev_min, dev_max = _num(new.attributes.get("min_temp")), _num(new.attributes.get("max_temp"))
        for value in temps.values():
            if (dev_min is not None and value <= dev_min + 0.01) or (dev_max is not None and value >= dev_max - 0.01):
                # Frost or window protection, or the proportional boost to the maximum
                return _ignored(entity_id, f"{value} at the device limit")
        return _Change(entity_id=entity_id, hvac_state=new.state, temps=temps)

    def _room_blocks(self, area_id: str, entity_id: str) -> str | None:
        """Why the room does not follow right now, None when it does."""
        if self._started_at is None:
            return "stopped"
        if not self.hass.is_running or time.monotonic() - self._started_at < FOLLOW_STARTUP_GRACE_S:
            return "starting"
        store = self.hass.data[DOMAIN]["store"]
        room = store.get_room(area_id) or {}
        if not room.get("follow_thermostat") or room.get("is_outdoor"):
            return "follow off"
        if not room.get("climate_control_enabled", True):
            return "room off"
        if not store.get_settings().get("climate_control_active", True):
            return "control off"
        live = ((self._coordinator.data or {}).get("rooms") or {}).get(area_id)
        if not live:
            return "no cycle yet"
        if live.get("window_open") or live.get("force_off"):
            return "window open or forced off"
        if self._coordinator.is_room_switching_off(area_id):
            return "switching off"
        if self._coordinator.is_device_busy(entity_id):
            return "valve protection or coil drying"
        return None

    @callback
    def _on_state_change(self, event: Event) -> None:
        entity_id: str = event.data["entity_id"]
        area_id = self._eid_to_area.get(entity_id)
        if area_id is None:
            return
        change = self._device_change(
            entity_id, event.data.get("old_state"), event.data.get("new_state"), event.context.parent_id
        )
        if change is None:
            return
        reason = self._room_blocks(area_id, entity_id)
        if reason is not None:
            _LOGGER.debug("Thermostat follow: '%s' changed, room '%s' not following (%s)", entity_id, area_id, reason)
            return
        self._pending[area_id] = change
        cancel = self._debounce.pop(area_id, None)
        if cancel is not None:
            cancel()
        self._debounce[area_id] = async_call_later(self.hass, FOLLOW_DEBOUNCE_S, self._make_adopt(area_id))

    def _make_adopt(self, area_id: str) -> Callable[[Any], Any]:
        async def _adopt(_now: Any) -> None:
            self._debounce.pop(area_id, None)
            change = self._pending.pop(area_id, None)
            if change is not None:
                await self.async_adopt(area_id, change)

        return _adopt

    # ─── Adoption ─────────────────────────────────────────────────

    async def async_adopt(self, area_id: str, change: _Change) -> None:
        """Hold the device's setpoint as the room target (the checks run again: time has passed)."""
        reason = self._room_blocks(area_id, change.entity_id)
        if reason is not None:
            _LOGGER.debug("Thermostat follow: room '%s' no longer following (%s)", area_id, reason)
            return
        store = self.hass.data[DOMAIN]["store"]
        room: dict = store.get_room(area_id) or {}
        live: dict = ((self._coordinator.data or {}).get("rooms") or {}).get(area_id) or {}
        sides = self._sides(room, live, change)
        climate_mode = room.get("climate_mode", CLIMATE_MODE_AUTO)
        if climate_mode == CLIMATE_MODE_HEAT_ONLY:
            sides.pop(SIDE_COOL, None)
        elif climate_mode == CLIMATE_MODE_COOL_ONLY:
            sides.pop(SIDE_HEAT, None)
        if not sides:
            return
        if climate_mode == CLIMATE_MODE_AUTO:
            kwargs = {ATTR_TARGET_TEMP_LOW: sides.get(SIDE_HEAT), ATTR_TARGET_TEMP_HIGH: sides.get(SIDE_COOL)}
        else:
            kwargs = {ATTR_TEMPERATURE: next(iter(sides.values()))}
        in_force = current_band(live, room)
        band = band_from_temperature_call(kwargs, climate_mode, lambda: in_force, room)
        if band is None:
            return
        if all(
            (want is None and have is None)
            or (want is not None and have is not None and abs(want - have) < _SAME_BAND_TOLERANCE)
            for want, have in zip(band, in_force, strict=True)
        ):
            return
        await async_store_room_override(self._coordinator, area_id, band[0], band[1], OVERRIDE_CUSTOM)
        value = next(iter(sides.values()))
        self.last_adoption[area_id] = {"entity_id": change.entity_id, "value": value, "at": time.time()}
        _LOGGER.info("Room '%s': adopted %.1f °C set on '%s'", area_id, value, change.entity_id)
        await self._coordinator.async_request_refresh()

    def _room_target(self, room: dict, entity_id: str, value_ha: float) -> float:
        """The room temperature the user meant by a device setpoint (°C)."""
        value = ha_temp_to_celsius(self.hass, value_ha)
        if room.get("temperature_sensor"):
            # A direct-mode device runs at room target + offset against the room sensor
            value -= get_setpoint_offset(room.get("devices", []), entity_id)
        # Proportional mode sends its own valve settings; the user still means a room temperature
        return max(MIN_TARGET_TEMP, min(MAX_TARGET_TEMP, round(value * 2) / 2))

    def _sides(self, room: dict, live: dict, change: _Change) -> dict[str, float]:
        """Which side of the room's band the change sets: {heat|cool: °C}."""
        device = get_device_by_eid(room.get("devices", []), change.entity_id) or {}
        target = {key: self._room_target(room, change.entity_id, v) for key, v in change.temps.items()}
        if ATTR_TARGET_TEMP_LOW in target or ATTR_TARGET_TEMP_HIGH in target:
            sides = {}
            if ATTR_TARGET_TEMP_LOW in target:
                sides[SIDE_HEAT] = target[ATTR_TARGET_TEMP_LOW]
            if ATTR_TARGET_TEMP_HIGH in target:
                sides[SIDE_COOL] = target[ATTR_TARGET_TEMP_HIGH]
            return sides
        value = target[ATTR_TEMPERATURE]
        if is_trv_type(device) or change.hvac_state == "heat":
            return {SIDE_HEAT: value}
        if change.hvac_state == "cool":
            return {SIDE_COOL: value}
        # heat_cool/auto with one setpoint: the way the room is running, else the nearer side
        mode = live.get("mode")
        if mode == MODE_HEATING:
            return {SIDE_HEAT: value}
        if mode == MODE_COOLING:
            return {SIDE_COOL: value}
        climate_mode = room.get("climate_mode", CLIMATE_MODE_AUTO)
        if climate_mode == CLIMATE_MODE_HEAT_ONLY:
            return {SIDE_HEAT: value}
        if climate_mode == CLIMATE_MODE_COOL_ONLY:
            return {SIDE_COOL: value}
        heat, cool = current_band(live, room)
        if heat is not None and cool is not None and abs(value - cool) < abs(value - heat):
            return {SIDE_COOL: value}
        return {SIDE_HEAT: value}
