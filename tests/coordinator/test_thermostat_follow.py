"""Coordinator hooks for thermostat follow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.roommind.managers.ac_coil_dry_manager import CoilDryState
from custom_components.roommind.managers.thermostat_follow_manager import ThermostatFollowManager

from .conftest import SAMPLE_ROOM, _create_coordinator, _make_store_mock, make_mock_states_get


def test_device_busy_during_valve_protection_or_coil_drying(hass, mock_config_entry):
    coordinator = _create_coordinator(hass, mock_config_entry)
    assert coordinator.is_device_busy("climate.trv") is False
    coordinator._valve_manager._cycling["climate.trv"] = 0.0
    assert coordinator.is_device_busy("climate.trv") is True
    coordinator._coil_dry_manager._states["climate.ac"] = CoilDryState()
    assert coordinator.is_device_busy("climate.ac") is False
    coordinator._coil_dry_manager._states["climate.ac"] = CoilDryState(phase="blow")
    assert coordinator.is_device_busy("climate.ac") is True


@pytest.mark.asyncio
async def test_room_switching_off_until_the_cycle_sends_it(hass, mock_config_entry):
    store = _make_store_mock({"living_room_abc12345": SAMPLE_ROOM})
    store.async_update_room = AsyncMock()
    hass.data = {"roommind": {"store": store}}
    coordinator = _create_coordinator(hass, mock_config_entry)
    coordinator.async_request_refresh = AsyncMock()
    assert coordinator.is_room_switching_off("living_room_abc12345") is False
    await coordinator.async_turn_room_off("living_room_abc12345")
    assert coordinator.is_room_switching_off("living_room_abc12345") is True


@pytest.mark.asyncio
async def test_every_cycle_syncs_followed_devices(hass, mock_config_entry):
    rooms = {"living_room_abc12345": SAMPLE_ROOM}
    hass.data = {"roommind": {"store": _make_store_mock(rooms)}}
    hass.states.get = MagicMock(side_effect=make_mock_states_get())
    hass.services.async_call = AsyncMock()
    coordinator = _create_coordinator(hass, mock_config_entry)
    assert isinstance(coordinator.thermostat_follow, ThermostatFollowManager)
    with patch.object(coordinator.thermostat_follow, "async_sync") as sync:
        await coordinator._async_update_data()
    sync.assert_called_once_with(rooms)
