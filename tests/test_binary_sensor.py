"""Tests for RoomMind binary sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.roommind.binary_sensor import (
    RoomMindCentralHeatingSensor,
    RoomMindCoverPausedSensor,
    _create_room_binary_sensors,
    async_setup_entry,
)
from custom_components.roommind.const import DOMAIN


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.data = {"rooms": {"living_room": {"cover_auto_paused": False}}}
    return coordinator


def test_cover_paused_off(mock_coordinator):
    """Binary sensor OFF when no user override active."""
    mock_coordinator.data = {"rooms": {"living_room": {"cover_auto_paused": False}}}
    sensor = RoomMindCoverPausedSensor(mock_coordinator, "living_room")
    assert sensor.is_on is False


def test_cover_paused_on(mock_coordinator):
    """Binary sensor ON when user override is active."""
    mock_coordinator.data = {"rooms": {"living_room": {"cover_auto_paused": True}}}
    sensor = RoomMindCoverPausedSensor(mock_coordinator, "living_room")
    assert sensor.is_on is True


def test_cover_paused_missing_room(mock_coordinator):
    """Binary sensor returns False when room doesn't exist."""
    mock_coordinator.data = {"rooms": {}}
    sensor = RoomMindCoverPausedSensor(mock_coordinator, "nonexistent")
    assert sensor.is_on is False


def test_cover_paused_missing_key(mock_coordinator):
    """Binary sensor returns False when cover_auto_paused key is missing."""
    mock_coordinator.data = {"rooms": {"living_room": {}}}
    sensor = RoomMindCoverPausedSensor(mock_coordinator, "living_room")
    assert sensor.is_on is False


def test_binary_sensor_unique_id_and_entity_id(mock_coordinator):
    """Binary sensor has correct unique_id and entity_id."""
    sensor = RoomMindCoverPausedSensor(mock_coordinator, "living_room")
    assert sensor.unique_id == "roommind_living_room_cover_paused"
    assert sensor.entity_id == "binary_sensor.roommind_living_room_cover_paused"


def test_create_room_binary_sensors(mock_coordinator):
    """Factory creates exactly one binary sensor per room."""
    sensors = _create_room_binary_sensors(mock_coordinator, "living_room")
    assert len(sensors) == 1
    assert isinstance(sensors[0], RoomMindCoverPausedSensor)


def test_cover_paused_coordinator_data_none(mock_coordinator):
    """Binary sensor returns False when coordinator.data is None (before first update)."""
    mock_coordinator.data = None
    sensor = RoomMindCoverPausedSensor(mock_coordinator, "living_room")
    assert sensor.is_on is False


@pytest.mark.asyncio
async def test_async_setup_entry_creates_entities_for_rooms_with_covers():
    """async_setup_entry creates binary sensors for rooms with covers configured."""
    coordinator = MagicMock()
    coordinator._binary_sensor_entity_areas = set()

    store = MagicMock()
    store.get_rooms.return_value = {
        "living_room": {"covers": ["cover.blinds"]},
        "bedroom": {},  # no covers — should be skipped
    }

    entry = MagicMock()
    entry.entry_id = "test_entry"

    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: coordinator, "store": store}}

    async_add_entities = MagicMock()

    await async_setup_entry(hass, entry, async_add_entities)

    # Callback stored on coordinator
    assert coordinator.async_add_binary_sensor_entities is async_add_entities
    # Only living_room has covers, so one room entity plus the central heating sensor
    async_add_entities.assert_called_once()
    entities = async_add_entities.call_args[0][0]
    assert len(entities) == 2
    assert isinstance(entities[0], RoomMindCoverPausedSensor)
    assert isinstance(entities[1], RoomMindCentralHeatingSensor)
    # Area tracked
    assert "living_room" in coordinator._binary_sensor_entity_areas
    assert "bedroom" not in coordinator._binary_sensor_entity_areas


@pytest.mark.asyncio
async def test_async_setup_entry_no_covers_only_central_heating():
    """Without covers only the global central heating sensor is created."""
    coordinator = MagicMock()
    coordinator._binary_sensor_entity_areas = set()

    store = MagicMock()
    store.get_rooms.return_value = {"bedroom": {}}

    entry = MagicMock()
    entry.entry_id = "test_entry"

    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: coordinator, "store": store}}

    async_add_entities = MagicMock()

    await async_setup_entry(hass, entry, async_add_entities)

    entities = async_add_entities.call_args[0][0]
    assert len(entities) == 1
    assert isinstance(entities[0], RoomMindCentralHeatingSensor)


def test_central_heating_sensor_reads_coordinator_state():
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = {"rooms": {}, "central_heating": {"enabled": False}}
    sensor = RoomMindCentralHeatingSensor(coordinator)
    assert sensor.unique_id == "roommind_central_heating"
    assert sensor.entity_id == "binary_sensor.roommind_central_heating"
    assert sensor.available is False
    coordinator.data = {
        "rooms": {},
        "central_heating": {
            "enabled": True,
            "on": True,
            "entity_id": "switch.boiler",
            "demanding_rooms": ["bad", "buero"],
            "demanding_count": 2,
            "min_rooms": 2,
            "pending": None,
            "pending_seconds": None,
        },
    }
    assert sensor.available is True
    assert sensor.is_on is True
    assert sensor.extra_state_attributes["demanding_rooms"] == ["bad", "buero"]
    coordinator.data = None
    assert sensor.is_on is False
