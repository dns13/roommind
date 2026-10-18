"""Override band helpers shared by the room climate entity and thermostat follow."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ATTR_TARGET_TEMP_HIGH, ATTR_TARGET_TEMP_LOW
from homeassistant.const import ATTR_TEMPERATURE

from ..const import (
    CLIMATE_MODE_AUTO,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DOMAIN,
    OVERRIDE_BOOST,
    is_override_active,
)
from .schedule_utils import mask_override_band, override_preset_band, override_until_for_room

if TYPE_CHECKING:
    from ..coordinator import RoomMindCoordinator


def current_band(live: dict, room: dict) -> tuple[float | None, float | None]:
    """Return the (heat, cool) band currently in force for this room.

    An active override wins. Otherwise it is the band the coordinator resolved
    last cycle (minus mold prevention, which is not part of the user's band).
    Falls back to the comfort band when no live targets exist yet or the room
    is forced off.
    """
    if is_override_active(room):
        return room.get("override_heat"), room.get("override_cool")
    heat = live.get("heat_target")
    cool = live.get("cool_target")
    if heat is None and cool is None:
        return override_preset_band(room, OVERRIDE_BOOST)
    if heat is not None and live.get("mold_prevention_active"):
        heat -= live.get("mold_prevention_delta") or 0
    return heat, cool


def band_from_temperature_call(
    kwargs: dict[str, Any],
    climate_mode: str,
    current: Callable[[], tuple[float | None, float | None]],
    room: dict,
) -> tuple[float | None, float | None] | None:
    """Turn the arguments of a set_temperature call into an override band, None without targets."""
    low = kwargs.get(ATTR_TARGET_TEMP_LOW)
    high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
    single = kwargs.get(ATTR_TEMPERATURE)
    if low is not None or high is not None:
        # A half-specified range keeps the other side of what is already in
        # force instead of dropping it, so a follow-up call cannot open a
        # one-sided band in between (#447).
        cur_heat, cur_cool = current()
        heat = low if low is not None else cur_heat
        cool = high if high is not None else cur_cool
        if (low is None) != (high is None) and heat is not None and cool is not None and cool < heat:
            if high is None:
                cool = heat
            else:
                heat = cool
        return heat, cool
    if single is None:
        return None
    if climate_mode == "cool_only":
        return None, single
    if climate_mode == "heat_only":
        return single, None
    # Auto: a bare `temperature` (legacy/external automation) must NOT
    # collapse to a single point (that is the cycling bug). Derive a
    # dead-band identically to the store migration.
    return single, max(single, room.get("comfort_cool", DEFAULT_COMFORT_COOL))


def fill_auto_band(heat: float | None, cool: float | None, room: dict) -> tuple[float | None, float | None]:
    """Give a one-sided band its missing side, the way a bare temperature widens in auto."""
    if heat is not None and cool is None:
        cool = max(heat, room.get("comfort_cool", DEFAULT_COMFORT_COOL))
    elif cool is not None and heat is None:
        heat = min(cool, room.get("comfort_heat", room.get("comfort_temp", DEFAULT_COMFORT_HEAT)))
    return heat, cool


async def async_store_room_override(
    coordinator: RoomMindCoordinator,
    area_id: str,
    heat: float | None,
    cool: float | None,
    override_type: str,
) -> None:
    """Store a manual override that holds for the room's override duration.

    The caller refreshes the coordinator once its own state is written.
    """
    store = coordinator.hass.data[DOMAIN]["store"]
    room: dict = store.get_room(area_id) or {}
    heat, cool = mask_override_band(heat, cool, room.get("climate_mode", CLIMATE_MODE_AUTO))
    await store.async_update_room(
        area_id,
        {
            "override_heat": heat,
            "override_cool": cool,
            "override_until": override_until_for_room(coordinator.hass, room),
            "override_type": override_type,
        },
    )
    coordinator.clear_override_seed(area_id)
