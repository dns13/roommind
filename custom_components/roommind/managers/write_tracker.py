"""Record RoomMind's own climate service calls per device.

Thermostat follow needs to tell a setpoint someone turned on the device from the
echo of a value RoomMind sent itself. The device state alone cannot say: Zigbee
and Z-Wave devices report a new setpoint seconds later and under a new context.

The call_service event fires before the service runs, so a write is on record
before any state echo of it can arrive. It also sees the writes that bypass the
MPC controller's command cache (valve protection, coil drying, master devices).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, callback

from ..const import FOLLOW_ECHO_HISTORY_S, ROOMMIND_CONTEXT_ID

_TEMP_KEYS = ("temperature", "target_temp_low", "target_temp_high")
_MAX_WRITES_PER_ENTITY = 8


@dataclass(frozen=True)
class WriteRecord:
    """One climate service call RoomMind made for an entity."""

    at: float  # monotonic
    service: str
    temps: dict[str, float] = field(default_factory=dict)


class RoomMindWriteTracker:
    """Remembers the recent climate writes RoomMind made, per entity."""

    def __init__(self) -> None:
        self._writes: dict[str, deque[WriteRecord]] = {}

    @callback
    def async_start(self, hass: HomeAssistant) -> Callable[[], None]:
        """Listen for RoomMind's climate service calls; returns the unsubscribe callback."""
        unsub: Callable[[], None] = hass.bus.async_listen(
            EVENT_CALL_SERVICE, self._on_call_service, event_filter=self._is_roommind_climate
        )
        return unsub

    @staticmethod
    @callback
    def _is_roommind_climate(event_data: dict) -> bool:
        return bool(event_data.get("domain") == "climate")

    @callback
    def _on_call_service(self, event: Event) -> None:
        if event.context.parent_id != ROOMMIND_CONTEXT_ID:
            return
        data = event.data.get("service_data") or {}
        self.note_write(data.get("entity_id"), str(event.data.get("service", "")), data)

    def note_write(self, entity_ids: str | list[str] | None, service: str, data: dict) -> None:
        """Record a write for each entity it went to."""
        if entity_ids is None:
            return
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        temps: dict[str, float] = {}
        for key in _TEMP_KEYS:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                temps[key] = float(value)
        record = WriteRecord(at=time.monotonic(), service=service, temps=temps)
        for eid in entity_ids:
            self._writes.setdefault(eid, deque(maxlen=_MAX_WRITES_PER_ENTITY)).append(record)

    def _recent(self, entity_id: str) -> list[WriteRecord]:
        writes = self._writes.get(entity_id)
        if not writes:
            return []
        cutoff = time.monotonic() - FOLLOW_ECHO_HISTORY_S
        while writes and writes[0].at < cutoff:
            writes.popleft()
        if not writes:
            del self._writes[entity_id]
            return []
        return list(writes)

    def last_write_age(self, entity_id: str) -> float | None:
        """Seconds since RoomMind's last climate call for the entity, None without one on record."""
        writes = self._recent(entity_id)
        if not writes:
            return None
        return time.monotonic() - writes[-1].at

    def matches_recent_write(self, entity_id: str, temps: dict[str, float], tolerance: float) -> bool:
        """True when any of the given setpoints is one RoomMind sent recently (a late echo).

        Compared across low/high/temperature: a range device can report a value
        RoomMind sent as the other attribute.
        """
        sent = [value for record in self._recent(entity_id) for value in record.temps.values()]
        return any(abs(value - other) <= tolerance for value in temps.values() for other in sent)
