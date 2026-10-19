"""Pre-heating/pre-cooling per room and per schedule block."""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.roommind.const import TargetTemps
from custom_components.roommind.control.mpc_controller import MPCController
from custom_components.roommind.control.mpc_optimizer import MPCOptimizer
from custom_components.roommind.control.thermal_model import RCModel, RoomModelManager
from custom_components.roommind.utils.schedule_utils import (
    _UNSET,
    make_preheat_lead_resolver,
    mask_preheat,
    parse_block_preheat,
)
from tests.control.conftest import build_hass, make_room

# ---------------------------------------------------------------------------
# Block data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, None),
        (False, 0.0),
        ("on", None),
        (" YES ", None),
        ("false", 0.0),
        ("Off", 0.0),
        (45, 2700.0),
        (45.5, 2730.0),
        ("30", 1800.0),
        (0, 0.0),
        (-10, 0.0),
    ],
)
def test_parse_block_preheat(raw, expected):
    assert parse_block_preheat(raw) == expected


@pytest.mark.parametrize("raw", [None, "maybe", float("nan"), [1]])
def test_parse_block_preheat_unusable(raw):
    assert parse_block_preheat(raw) is _UNSET


def _blocks(data_morning: dict, data_evening: dict | None = None) -> dict:
    day = [
        {"from": "06:00:00", "to": "08:00:00", "data": data_morning},
        {"from": "17:00:00", "to": "22:00:00", "data": data_evening or {}},
    ]
    return {d: day for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}


def _ts(hour: int, minute: int = 0) -> float:
    return dt_util.as_utc(datetime(2026, 1, 12, hour, minute, tzinfo=dt_util.get_default_time_zone())).timestamp()


def test_lead_resolver_room_default_and_block_override():
    blocks = _blocks({"preheat": 45}, {"preheat": False})
    lead = make_preheat_lead_resolver(blocks, {})
    assert lead(_ts(7)) == 2700.0  # morning block: 45 min
    assert lead(_ts(18)) == 0.0  # evening block: off
    assert lead(_ts(12)) is None  # outside blocks: the room's default (on)
    off_room = make_preheat_lead_resolver(_blocks({}, {"preheat": True}), {"preheat_enabled": False})
    assert off_room(_ts(7)) == 0.0  # no block value: room off
    assert off_room(_ts(18)) is None  # block switches it on


def test_lead_resolver_without_schedule():
    assert make_preheat_lead_resolver(None, {})(time.time()) is None
    assert make_preheat_lead_resolver({}, {"preheat_enabled": False})(time.time()) == 0.0


# ---------------------------------------------------------------------------
# Mask
# ---------------------------------------------------------------------------


def test_mask_hides_stricter_targets_beyond_their_lead():
    heat = [18.0, 18.0, 18.0, 21.0, 21.0, 17.0]
    cool = [27.0, 27.0, 27.0, 24.0, 24.0, 28.0]
    # 10-minute blocks; the comfort block (index 3, 30 min away) allows 20 min lead
    leads = [None, None, None, 1200.0, 1200.0, None]
    h, c = mask_preheat(heat, cool, leads, 600.0)
    assert h == [18.0, 18.0, 18.0, 18.0, 18.0, 17.0]
    assert c == [27.0, 27.0, 27.0, 27.0, 27.0, 28.0]
    # 20 minutes later the block start is within its lead
    h, _ = mask_preheat(heat[2:], cool[2:], leads[2:], 600.0)
    assert h[1] == 21.0


def test_mask_keeps_off_and_milder_targets():
    h, c = mask_preheat([None, 21.0, None], [None, 24.0, None], [0.0, 0.0, 0.0], 300.0)
    assert h == [None, None, None]
    assert c == [None, None, None]
    h, c = mask_preheat([21.0, 17.0], [24.0, 28.0], [0.0, 0.0], 300.0)
    assert (h, c) == ([21.0, 17.0], [24.0, 28.0])
    assert mask_preheat([], [], [], 300.0) == ([], [])


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


def _controller(preheat_lead, monkeypatch):
    """Eco 18 now, comfort 21 from 45 minutes on; captures what the optimizer is asked to reach."""
    start = time.time()

    def resolver(ts: float) -> TargetTemps:
        return TargetTemps(heat=21.0 if ts - start >= 45 * 60 else 18.0, cool=24.0 if ts - start >= 45 * 60 else 27.0)

    seen: dict = {}
    real_optimize = MPCOptimizer.optimize

    def optimize(self, *args, **kwargs):
        seen.update(kwargs)
        return real_optimize(self, *args, **kwargs)

    monkeypatch.setattr(MPCOptimizer, "optimize", optimize)
    mgr = RoomModelManager()
    mgr.get_model = MagicMock(return_value=RCModel(C=1.0, U=0.1, Q_heat=4.0, Q_cool=4.0))
    ctrl = MPCController(
        build_hass(),
        make_room(),
        model_manager=mgr,
        outdoor_temp=10.0,
        settings={},
        has_external_sensor=True,
        target_resolver=resolver,
        preheat_lead=preheat_lead,
    )
    ctrl._evaluate_mpc(18.5, TargetTemps(heat=18.0, cool=27.0))
    return ctrl, seen


def test_controller_plans_towards_the_block_by_default(monkeypatch):
    ctrl, seen = _controller(None, monkeypatch)
    assert max(seen["heat_target_series"]) == 21.0
    assert min(seen["cool_target_series"]) == 24.0


def test_controller_hides_the_block_with_preheat_off(monkeypatch):
    ctrl, seen = _controller(lambda ts: 0.0, monkeypatch)
    assert max(seen["heat_target_series"]) == 18.0
    assert min(seen["cool_target_series"]) == 27.0
    # the lead target of direct-mode devices stays at the target in force
    assert ctrl._lead_heat == 18.0


def test_controller_shows_the_block_within_its_lead(monkeypatch):
    # 60 minutes allowed, the block is 45 minutes away: visible
    _, seen = _controller(lambda ts: 3600.0, monkeypatch)
    assert max(seen["heat_target_series"]) == 21.0
    # 30 minutes allowed: not yet
    _, seen = _controller(lambda ts: 1800.0, monkeypatch)
    assert max(seen["heat_target_series"]) == 18.0
