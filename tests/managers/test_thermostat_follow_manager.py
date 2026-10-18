"""Tests for following a setpoint turned on a room's thermostat or AC."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import UnitOfTemperature
from homeassistant.core import Context, State

from custom_components.roommind.const import DOMAIN, FOLLOW_SETTLE_S, MAX_TARGET_TEMP, make_roommind_context
from custom_components.roommind.managers import thermostat_follow_manager as tfm
from custom_components.roommind.managers import write_tracker as wt
from custom_components.roommind.managers.thermostat_follow_manager import (
    FOLLOW_DEBOUNCE_S,
    FOLLOW_STARTUP_GRACE_S,
    ThermostatFollowManager,
    changed_setpoints,
)
from custom_components.roommind.managers.write_tracker import RoomMindWriteTracker

TRV = {"entity_id": "climate.trv", "type": "trv"}
AC = {"entity_id": "climate.ac", "type": "ac"}


def _room(**overrides):
    room = {
        "area_id": "living",
        "climate_mode": "heat_only",
        "devices": [TRV],
        "temperature_sensor": "sensor.living_temp",
        "comfort_heat": 21.0,
        "comfort_cool": 24.0,
        "eco_heat": 17.0,
        "eco_cool": 27.0,
        "override_heat": None,
        "override_cool": None,
        "override_until": None,
        "override_type": None,
        "climate_control_enabled": True,
        "follow_thermostat": True,
        "schedules": [],
    }
    room.update(overrides)
    return room


def _live(**overrides):
    live = {"heat_target": 21.0, "cool_target": None, "mode": "idle", "window_open": False, "force_off": False}
    live.update(overrides)
    return live


def _state(temp=21.0, hvac="heat", entity_id="climate.trv", **attrs):
    attributes = {"temperature": temp, "min_temp": 5.0, "max_temp": 30.0, "target_temp_step": 0.5}
    attributes.update(attrs)
    if temp is None:
        attributes.pop("temperature")
    return State(entity_id, hvac, attributes)


def _event(old, new, context=None, entity_id="climate.trv"):
    event = MagicMock()
    event.data = {"entity_id": entity_id, "old_state": old, "new_state": new}
    event.context = context or Context()
    return event


class Env:
    """A started manager with one room, its store, live data and a controllable clock."""

    def __init__(self, monkeypatch):
        self.now = [10_000.0]
        monkeypatch.setattr(tfm.time, "monotonic", lambda: self.now[0])
        monkeypatch.setattr(wt.time, "monotonic", lambda: self.now[0])
        self.rooms = {"living": _room()}
        self.settings: dict = {}
        self.store = MagicMock()
        self.store.get_rooms.side_effect = lambda: self.rooms
        self.store.get_room.side_effect = lambda area_id: self.rooms.get(area_id)  # noqa: PLW0108 - rooms is replaced
        self.store.get_settings.side_effect = lambda: self.settings
        self.store.async_update_room = AsyncMock()
        self.hass = MagicMock()
        self.hass.is_running = True
        self.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
        self.hass.data = {DOMAIN: {"store": self.store}}
        self.coordinator = MagicMock()
        self.coordinator.hass = self.hass
        self.coordinator.write_tracker = RoomMindWriteTracker()
        self.coordinator.data = {"rooms": {"living": _live()}}
        self.coordinator.is_room_switching_off.return_value = False
        self.coordinator.is_device_busy.return_value = False
        self.coordinator.async_request_refresh = AsyncMock()
        self.unsub = MagicMock()
        self.track = patch.object(tfm, "async_track_state_change_event", return_value=self.unsub).start()
        self.timers: list[tuple[float, object, MagicMock]] = []

        def call_later(_hass, delay, action):
            cancel = MagicMock()
            self.timers.append((delay, action, cancel))
            return cancel

        self.call_later = patch.object(tfm, "async_call_later", side_effect=call_later).start()
        self.manager = ThermostatFollowManager(self.hass, self.coordinator)
        self.stop = self.manager.async_start()
        self.now[0] += FOLLOW_STARTUP_GRACE_S

    @property
    def live(self) -> dict:
        return self.coordinator.data["rooms"]["living"]

    def change(self, old, new, context=None, entity_id="climate.trv"):
        self.manager._on_state_change(_event(old, new, context, entity_id))

    async def fire(self):
        """Run the last debounce timer."""
        _, action, _ = self.timers[-1]
        await action(None)

    def written(self) -> dict:
        return self.store.async_update_room.call_args[0][1]


@pytest.fixture
def env(monkeypatch):
    environment = Env(monkeypatch)
    yield environment
    patch.stopall()


# ─── Subscription ──────────────────────────────────────────────────


def test_start_subscribes_follow_rooms_only(env):
    env.track.assert_called_once()
    assert env.track.call_args[0][1] == ["climate.trv"]
    assert env.stop == env.manager.async_stop


def test_sync_skips_rooms_without_follow_outdoor_and_shared_devices(env):
    env.rooms = {
        "living": _room(devices=[TRV, {"entity_id": "climate.shared"}, {"type": "trv"}]),
        "office": _room(devices=[{"entity_id": "climate.shared"}, AC]),
        "garden": _room(is_outdoor=True, devices=[{"entity_id": "climate.garden"}]),
        "hall": _room(follow_thermostat=False, devices=[{"entity_id": "climate.hall"}]),
    }
    env.manager.async_sync(env.rooms)
    env.unsub.assert_called_once()
    assert sorted(env.track.call_args[0][1]) == ["climate.ac", "climate.trv"]


def test_sync_unchanged_keeps_subscription(env):
    env.manager.async_sync(env.rooms)
    env.track.assert_called_once()
    env.unsub.assert_not_called()


def test_sync_without_follow_rooms_unsubscribes(env):
    env.rooms = {"living": _room(follow_thermostat=False)}
    env.manager.async_sync(env.rooms)
    env.unsub.assert_called_once()
    env.track.assert_called_once()


def test_sync_before_start_does_nothing(env):
    manager = ThermostatFollowManager(env.hass, env.coordinator)
    manager.async_sync(env.rooms)
    env.track.assert_called_once()  # only the started env manager


def test_stop_unsubscribes_and_cancels_pending(env):
    env.change(_state(21.0), _state(23.0))
    _, _, cancel = env.timers[-1]
    env.manager.async_stop()
    env.unsub.assert_called_once()
    cancel.assert_called_once()
    assert env.manager._pending == {}
    env.manager.async_stop()  # twice is fine


def test_sync_drops_pending_of_room_no_longer_followed(env):
    env.change(_state(21.0), _state(23.0))
    _, _, cancel = env.timers[-1]
    env.rooms = {"living": _room(follow_thermostat=False)}
    env.manager.async_sync(env.rooms)
    cancel.assert_called_once()
    assert env.manager._pending == {}


def test_event_for_unfollowed_entity_ignored(env):
    env.change(_state(21.0, entity_id="climate.x"), _state(23.0, entity_id="climate.x"), entity_id="climate.x")
    assert env.timers == []


# ─── Device checks ─────────────────────────────────────────────────


def test_changed_setpoints():
    old = _state(21.0, target_temp_low=None)
    new = _state(21.4, target_temp_low=19.0)
    assert changed_setpoints(old, new, 0.3) == {"temperature": 21.4}
    assert changed_setpoints(old, new, 0.5) == {}
    assert changed_setpoints(old, new, 0.5, appeared=True) == {"target_temp_low": 19.0}


def test_tado_switched_on_from_off_without_old_setpoint_is_followed(env):
    """tado over HomeKit reports no setpoint while off: off {temperature: None} -> heat {temperature: 20}."""
    env.change(_state(None, hvac="off", temperature=None), _state(20.0, hvac="heat"))
    assert env.manager._pending["living"].temps == {"temperature": 20.0}


def test_setpoint_appearing_without_leaving_off_or_schedule_ignored(env):
    env.change(_state(None, temperature=None), _state(20.0))
    assert env.timers == []


@pytest.mark.parametrize(
    ("old", "new", "context"),
    [
        (None, _state(23.0), None),
        (_state(21.0), None, None),
        (State("climate.trv", "unavailable", {}), _state(23.0), None),
        (_state(21.0), State("climate.trv", "unknown", {}), None),
        (_state(21.0, hvac="off"), _state(23.0), make_roommind_context()),
        (_state(21.0, hvac="heat_cool"), _state(23.0), make_roommind_context()),
        (_state(21.0, hvac="heat"), _state(23.0, hvac="cool"), None),
        (_state(21.0, hvac="heat"), _state(23.0, hvac="off"), None),
        (_state(21.0, hvac="heat"), _state(18.0, hvac="heat_cool"), None),
        (_state(21.0, hvac="heat"), _state(18.0, hvac="auto"), None),
        (_state(21.0, hvac="off"), _state(18.0, hvac="heat_cool"), None),
        (_state(21.0, hvac="fan_only"), _state(23.0, hvac="fan_only"), None),
        (_state(21.0), _state(23.0, preset_mode="boost"), None),
        (_state(21.0), _state(21.2), None),
        (_state(21.0), _state(None), None),
        (_state(21.0), _state(5.0), None),
        (_state(21.0), _state(30.0), None),
    ],
    ids=[
        "no-old",
        "no-new",
        "was-unavailable",
        "now-unknown",
        "roommind-context-from-off",
        "roommind-context-mode-change",
        "heat-to-cool",
        "switched-off",
        "back-to-device-schedule",
        "back-to-device-auto",
        "off-to-device-schedule",
        "fan-only",
        "preset-change",
        "below-step",
        "attribute-gone",
        "at-min",
        "at-max",
    ],
)
def test_not_a_user_change(env, old, new, context):
    env.change(old, new, context)
    assert env.timers == []


@pytest.mark.parametrize(("old_hvac", "new_hvac"), [("heat_cool", "heat"), ("auto", "heat"), ("auto", "cool")])
def test_dial_leaving_the_device_schedule_is_followed(env, old_hvac, new_hvac):
    """tado over HomeKit: its own schedule shows as heat_cool, turning the dial switches to heat."""
    env.change(_state(18.0, hvac=old_hvac), _state(22.0, hvac=new_hvac))
    assert len(env.timers) == 1
    assert env.manager._pending["living"].hvac_state == new_hvac


def test_leaving_the_device_schedule_right_after_roommind_write_ignored(env):
    """RoomMind switching a tado from its schedule to heat is no turn of the dial."""
    env.coordinator.write_tracker.note_write("climate.trv", "set_hvac_mode", {"hvac_mode": "heat"})
    env.now[0] += FOLLOW_SETTLE_S - 1
    env.change(_state(18.0, hvac="heat_cool"), _state(22.0, hvac="heat"))
    assert env.timers == []


@pytest.mark.parametrize(("old_hvac", "new_hvac"), [("off", "heat"), ("off", "off")])
def test_knob_on_an_idled_off_device_is_followed(env, old_hvac, new_hvac):
    """RoomMind idles TRVs off; turning the knob switches it on or only moves its setpoint."""
    env.change(_state(21.0, hvac=old_hvac), _state(23.0, hvac=new_hvac))
    assert len(env.timers) == 1


@pytest.mark.parametrize(("old_hvac", "new_hvac"), [("off", "heat"), ("off", "off")])
def test_off_device_right_after_roommind_write_ignored(env, old_hvac, new_hvac):
    """Switched on by RoomMind, a device may restore a setpoint RoomMind no longer remembers."""
    env.coordinator.write_tracker.note_write("climate.trv", "set_hvac_mode", {"hvac_mode": "heat"})
    env.now[0] += FOLLOW_SETTLE_S - 1
    env.change(_state(21.0, hvac=old_hvac), _state(23.0, hvac=new_hvac))
    assert env.timers == []


def test_echo_under_roommind_context_ignored(env):
    env.coordinator.write_tracker.note_write("climate.trv", "set_temperature", {"temperature": 22.6})
    env.change(_state(21.0), _state(23.0), make_roommind_context())
    assert env.timers == []


def test_dial_reported_under_roommind_context_is_followed(env):
    """tado over HomeKit reports the turned dial only with RoomMind's next write, under its context."""
    env.coordinator.write_tracker.note_write("climate.trv", "set_temperature", {"temperature": 19.0})
    env.change(_state(19.0), _state(23.0), make_roommind_context())
    assert len(env.timers) == 1


def test_echo_of_roommind_write_ignored(env):
    env.coordinator.write_tracker.note_write("climate.trv", "set_temperature", {"temperature": 23.0})
    env.now[0] += FOLLOW_SETTLE_S + 60
    env.change(_state(21.0), _state(23.0))
    assert env.timers == []


def test_rounded_value_right_after_write_ignored(env):
    """Right after RoomMind wrote 22.6 the device may settle one step away (23.0)."""
    env.coordinator.write_tracker.note_write("climate.trv", "set_temperature", {"temperature": 22.6})
    env.now[0] += 5
    env.change(_state(21.0), _state(23.0))
    assert env.timers == []
    env.now[0] += FOLLOW_SETTLE_S
    env.change(_state(21.0), _state(23.5))
    assert len(env.timers) == 1


def test_user_change_after_unrelated_write_is_followed(env):
    env.coordinator.write_tracker.note_write("climate.trv", "set_temperature", {"temperature": 19.0})
    env.now[0] += 2
    env.change(_state(19.0), _state(23.0))
    assert len(env.timers) == 1


# ─── Room checks ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "block",
    [
        "grace",
        "not-running",
        "follow-off",
        "outdoor",
        "room-off",
        "control-off",
        "no-live",
        "window",
        "force-off",
        "switching-off",
        "busy",
    ],
)
def test_room_blocks_following(env, block):
    if block == "grace":
        env.now[0] -= 1
    elif block == "not-running":
        env.hass.is_running = False
    elif block == "follow-off":
        env.rooms["living"]["follow_thermostat"] = False
    elif block == "outdoor":
        env.rooms["living"]["is_outdoor"] = True
    elif block == "room-off":
        env.rooms["living"]["climate_control_enabled"] = False
    elif block == "control-off":
        env.settings["climate_control_active"] = False
    elif block == "no-live":
        env.coordinator.data = None
    elif block == "window":
        env.live["window_open"] = True
    elif block == "force-off":
        env.live["force_off"] = True
    elif block == "switching-off":
        env.coordinator.is_room_switching_off.return_value = True
    else:
        env.coordinator.is_device_busy.return_value = True
    env.change(_state(21.0), _state(23.0))
    assert env.timers == []


def test_stopped_manager_blocks(env):
    env.manager.async_stop()
    assert env.manager._room_blocks("living", "climate.trv") == "stopped"


@pytest.mark.asyncio
async def test_room_blocked_by_the_time_debounce_ends(env):
    env.change(_state(21.0), _state(23.0))
    env.live["window_open"] = True
    await env.fire()
    env.store.async_update_room.assert_not_called()


# ─── Debounce and adoption ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_knob_turn_adopts_last_value_once(env):
    env.change(_state(21.0), _state(22.0))
    env.change(_state(22.0), _state(23.0))
    assert [delay for delay, _, _ in env.timers] == [FOLLOW_DEBOUNCE_S, FOLLOW_DEBOUNCE_S]
    env.timers[0][2].assert_called_once()
    await env.fire()
    assert env.written() == {
        "override_heat": 23.0,
        "override_cool": None,
        "override_until": None,
        "override_type": "custom",
    }
    env.coordinator.clear_override_seed.assert_called_once_with("living")
    env.coordinator.async_request_refresh.assert_awaited_once()
    assert env.manager.last_adoption["living"]["entity_id"] == "climate.trv"
    assert env.manager.last_adoption["living"]["value"] == 23.0
    assert env.manager._debounce == {}
    await env.fire()  # a timer firing without anything pending
    env.store.async_update_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_adoption_holds_until_next_schedule_change(env):
    next_event = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    schedule = MagicMock()
    schedule.state = "off"
    schedule.attributes = {"next_event": next_event}
    env.hass.states.get.return_value = schedule
    env.rooms["living"]["schedules"] = [{"entity_id": "schedule.living"}]
    env.change(_state(21.0), _state(23.0))
    await env.fire()
    assert env.written()["override_until"] == next_event.timestamp()


@pytest.mark.asyncio
async def test_direct_mode_offset_subtracted_with_room_sensor(env):
    env.rooms["living"]["devices"] = [{**TRV, "setpoint_mode": "direct", "setpoint_offset": 1.5}]
    env.change(_state(21.0), _state(24.0))
    await env.fire()
    assert env.written()["override_heat"] == 22.5


@pytest.mark.asyncio
async def test_direct_mode_offset_ignored_without_room_sensor(env):
    env.rooms["living"].update(
        devices=[{**TRV, "setpoint_mode": "direct", "setpoint_offset": 1.5}], temperature_sensor=""
    )
    env.change(_state(21.0), _state(24.0))
    await env.fire()
    assert env.written()["override_heat"] == 24.0


@pytest.mark.asyncio
async def test_fahrenheit_rounded_and_clamped(env):
    env.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    env.change(_state(70.0, max_temp=90.0, min_temp=40.0), _state(73.0, max_temp=90.0, min_temp=40.0))
    await env.fire()
    assert env.written()["override_heat"] == 23.0  # 22.78 °C
    env.change(_state(70.0, max_temp=99.0, min_temp=40.0), _state(97.0, max_temp=99.0, min_temp=40.0))
    await env.fire()
    assert env.written()["override_heat"] == MAX_TARGET_TEMP  # 36.1 °C


@pytest.mark.asyncio
async def test_same_band_is_no_change(env):
    """A direct-mode device RoomMind set to target + offset reads back as the target in force."""
    env.live["heat_target"] = 23.0
    env.change(_state(21.0), _state(23.0))
    await env.fire()
    env.store.async_update_room.assert_not_called()
    env.coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_room_trv_sets_heat_side_and_keeps_cool(env):
    env.rooms["living"]["climate_mode"] = "auto"
    env.live.update(heat_target=21.0, cool_target=24.0)
    env.change(_state(21.0), _state(22.5))
    await env.fire()
    assert (env.written()["override_heat"], env.written()["override_cool"]) == (22.5, 24.0)


@pytest.mark.asyncio
async def test_auto_room_trv_above_cool_side_pushes_it_up(env):
    env.rooms["living"]["climate_mode"] = "auto"
    env.live.update(heat_target=21.0, cool_target=24.0)
    env.change(_state(21.0), _state(25.0))
    await env.fire()
    assert (env.written()["override_heat"], env.written()["override_cool"]) == (25.0, 25.0)


@pytest.mark.asyncio
async def test_cool_only_room_ignores_trv(env):
    env.rooms["living"]["climate_mode"] = "cool_only"
    env.change(_state(21.0), _state(23.0))
    await env.fire()
    env.store.async_update_room.assert_not_called()


def _ac_room(env, climate_mode="auto"):
    env.rooms["living"].update(devices=[AC], climate_mode=climate_mode)
    env.manager.async_sync(env.rooms)
    env.live.update(heat_target=21.0, cool_target=25.0)


async def _ac_change(env, old, new):
    env.change(old, new, entity_id="climate.ac")
    await env.fire()
    return env.written()["override_heat"], env.written()["override_cool"]


@pytest.mark.asyncio
async def test_ac_in_cool_sets_cool_side(env):
    _ac_room(env)
    band = await _ac_change(env, _state(25.0, "cool", "climate.ac"), _state(23.0, "cool", "climate.ac"))
    assert band == (21.0, 23.0)


@pytest.mark.asyncio
async def test_ac_in_heat_sets_heat_side(env):
    _ac_room(env)
    band = await _ac_change(env, _state(21.0, "heat", "climate.ac"), _state(22.0, "heat", "climate.ac"))
    assert band == (22.0, 25.0)


@pytest.mark.asyncio
async def test_ac_range_sets_both_sides(env):
    _ac_room(env)
    old = _state(None, "heat_cool", "climate.ac", target_temp_low=21.0, target_temp_high=25.0)
    new = _state(None, "heat_cool", "climate.ac", target_temp_low=20.0, target_temp_high=26.0)
    assert await _ac_change(env, old, new) == (20.0, 26.0)


@pytest.mark.asyncio
async def test_ac_range_one_side(env):
    _ac_room(env)
    old = _state(None, "heat_cool", "climate.ac", target_temp_low=21.0, target_temp_high=25.0)
    new = _state(None, "heat_cool", "climate.ac", target_temp_low=21.0, target_temp_high=24.0)
    assert await _ac_change(env, old, new) == (21.0, 24.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("climate_mode", "mode", "value", "expected"),
    [
        ("auto", "heating", 23.0, (23.0, 25.0)),
        ("auto", "cooling", 23.0, (21.0, 23.0)),
        ("auto", "idle", 24.0, (21.0, 24.0)),
        ("auto", "idle", 22.0, (22.0, 25.0)),
        ("heat_only", "idle", 23.0, (23.0, None)),
        ("cool_only", "idle", 23.0, (None, 23.0)),
    ],
)
async def test_ac_heat_cool_single_setpoint_side(env, climate_mode, mode, value, expected):
    _ac_room(env, climate_mode)
    env.live["mode"] = mode
    if climate_mode == "heat_only":
        env.live["cool_target"] = None
    if climate_mode == "cool_only":
        env.live["heat_target"] = None
    band = await _ac_change(env, _state(20.0, "auto", "climate.ac"), _state(value, "auto", "climate.ac"))
    assert band == expected


@pytest.mark.asyncio
async def test_no_targets_left_adopts_nothing(env):
    """A one-sided change that the room's climate mode drops leaves nothing to hold."""
    _ac_room(env, "heat_only")
    old = _state(None, "heat_cool", "climate.ac", target_temp_low=21.0, target_temp_high=25.0)
    new = _state(None, "heat_cool", "climate.ac", target_temp_low=21.0, target_temp_high=24.0)
    env.change(old, new, entity_id="climate.ac")
    await env.fire()
    env.store.async_update_room.assert_not_called()


@pytest.mark.asyncio
async def test_band_none_adopts_nothing(env):
    with patch.object(tfm, "band_from_temperature_call", return_value=None):
        env.change(_state(21.0), _state(23.0))
        await env.fire()
    env.store.async_update_room.assert_not_called()
