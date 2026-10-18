"""Tests for the record of RoomMind's own climate writes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context

from custom_components.roommind.const import FOLLOW_ECHO_HISTORY_S, make_roommind_context
from custom_components.roommind.managers import write_tracker as wt
from custom_components.roommind.managers.write_tracker import RoomMindWriteTracker


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(wt.time, "monotonic", lambda: now[0])
    return now


def _event(service: str, data: dict, context: Context | None = None) -> MagicMock:
    event = MagicMock()
    event.context = context or make_roommind_context()
    event.data = {"domain": "climate", "service": service, "service_data": data}
    return event


def test_start_listens_to_climate_calls_only():
    hass = MagicMock()
    unsub = MagicMock()
    hass.bus.async_listen.return_value = unsub
    tracker = RoomMindWriteTracker()
    assert tracker.async_start(hass) is unsub
    event_type, handler = hass.bus.async_listen.call_args[0]
    assert event_type == EVENT_CALL_SERVICE
    event_filter = hass.bus.async_listen.call_args[1]["event_filter"]
    assert event_filter({"domain": "climate"}) is True
    assert event_filter({"domain": "cover"}) is False
    handler(_event("set_temperature", {"entity_id": "climate.trv", "temperature": 21.5}))
    assert tracker.last_write_age("climate.trv") is not None


def test_records_roommind_writes_only(clock):
    tracker = RoomMindWriteTracker()
    tracker._on_call_service(_event("set_temperature", {"entity_id": "climate.trv", "temperature": 22.0}, Context()))
    assert tracker.last_write_age("climate.trv") is None
    tracker._on_call_service(_event("set_temperature", {"entity_id": "climate.trv", "temperature": 22.0}))
    clock[0] += 12
    assert tracker.last_write_age("climate.trv") == 12


def test_entity_list_and_missing_entity(clock):
    tracker = RoomMindWriteTracker()
    tracker._on_call_service(_event("set_hvac_mode", {"hvac_mode": "heat"}))
    tracker._on_call_service(_event("set_hvac_mode", {"entity_id": ["climate.a", "climate.b"], "hvac_mode": "off"}))
    assert tracker.last_write_age("climate.a") == 0
    assert tracker.last_write_age("climate.b") == 0
    assert tracker.last_write_age("climate.c") is None


def test_matches_recent_write_within_tolerance_across_keys(clock):
    tracker = RoomMindWriteTracker()
    tracker.note_write("climate.ac", "set_temperature", {"target_temp_low": 20.0, "target_temp_high": 25.0})
    tracker.note_write("climate.ac", "set_temperature", {"temperature": 23.0, "hvac_mode": "heat"})
    assert tracker.matches_recent_write("climate.ac", {"temperature": 20.2}, 0.3)
    assert tracker.matches_recent_write("climate.ac", {"target_temp_low": 23.0}, 0.3)
    assert not tracker.matches_recent_write("climate.ac", {"temperature": 21.0}, 0.3)
    assert not tracker.matches_recent_write("climate.other", {"temperature": 20.0}, 0.3)


def test_ignores_non_numeric_temperatures(clock):
    tracker = RoomMindWriteTracker()
    tracker.note_write("climate.trv", "set_temperature", {"temperature": True, "target_temp_low": "20"})
    assert not tracker.matches_recent_write("climate.trv", {"temperature": 1.0}, 0.5)
    assert tracker.last_write_age("climate.trv") == 0


def test_keeps_last_writes_only(clock):
    tracker = RoomMindWriteTracker()
    for i in range(10):
        tracker.note_write("climate.trv", "set_temperature", {"temperature": 10.0 + i})
    assert not tracker.matches_recent_write("climate.trv", {"temperature": 11.0}, 0.1)
    assert tracker.matches_recent_write("climate.trv", {"temperature": 12.0}, 0.1)


def test_old_writes_expire(clock):
    tracker = RoomMindWriteTracker()
    tracker.note_write("climate.trv", "set_temperature", {"temperature": 21.0})
    clock[0] += FOLLOW_ECHO_HISTORY_S / 2
    tracker.note_write("climate.trv", "set_temperature", {"temperature": 22.0})
    clock[0] += FOLLOW_ECHO_HISTORY_S / 2 + 1
    assert not tracker.matches_recent_write("climate.trv", {"temperature": 21.0}, 0.1)
    assert tracker.matches_recent_write("climate.trv", {"temperature": 22.0}, 0.1)
    clock[0] += FOLLOW_ECHO_HISTORY_S
    assert tracker.last_write_age("climate.trv") is None
    assert "climate.trv" not in tracker._writes
